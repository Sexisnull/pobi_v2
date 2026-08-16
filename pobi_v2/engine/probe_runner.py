"""链路连通性探针（轻量快路径）。

区别于 ``deadend_runner.run_deadend_agent``（完整 AI 自主渗透系统，含多智能体
协作、avfs 持久化文件系统、RAG、ADaPT 规划、ValidationGate、ReporterAgent），
本模块用于 ``Task.kind == "probe"`` 的整体链路连通验证，目标只有一个：

    Worker 在线 → 共享 Kali 沙箱可达 → LLM 可调用 → 在 Kali 内用 curl 访问授权目标
    → 拿到 HTTP 响应 → LLM 一句话解读 → 结束。

设计原则（对应此前暴露的三个问题）：
1. **不碰 avfs / 多智能体**：直接在共享 Kali 沙箱执行 curl，绕开 dev 环境未挂载的
   avfs 与重型 DeadEndAgent 初始化（曾导致 Worker 卡死离线）。
2. **全程走 Kali**：网络访问全部在共享 Kali 容器内完成，不使用宿主机 requester。
3. **必须很快结束**：默认 1 次 curl + 1 次 LLM 解读，max_turns 退化为固定 1 轮，
   调用方用 ``asyncio.wait_for`` 套硬超时（默认 90s），不继承渗透任务的 50 轮 / 6h。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any
from uuid import UUID

from pobi_v2.core.config import settings
from pobi_v2.db.models import Task, Target

logger = logging.getLogger(__name__)

# 探针硬超时（秒）：整体（Kali curl + LLM 解读）超过即失败，绝不挂死。
PROBE_HARD_TIMEOUT = 90


def _build_curl_command(url: str) -> str:
    """构造一条只读的连通性探测 curl（HEAD 式，不下载 body，取状态码与标题片段）。"""
    # -sS 静默但保留错误；-o /dev/null 不下载；-w 输出状态码/耗时；
    # --max-time 30 防止目标无响应挂住；-I 仅取响应头（更轻、不触发重负载）。
    return (
        f"curl -sS -I --max-time 30 -o /dev/null "
        f"-w 'HTTP_CODE=%{{http_code}} TIME=%{{time_total}}s SIZE=%{{size_download}}\\n' "
        f"'{url}'"
    )


async def run_probe_agent(
    *,
    task: Task,
    target: Target,
    task_id: UUID,
    auto_approve: bool = True,
) -> dict:
    """在共享 Kali 沙箱里 curl 授权目标，并用 LLM 给出一句连通性结论。

    返回与 ``run_deadend_agent`` 兼容的产出字典（``summary`` / ``confidence`` /
    ``structured_report`` / ``findings``），使 ``executor.py`` 落库逻辑无需分支。
    """
    url = target.url
    logger.info("[probe %s] 开始在共享 Kali 中探测目标 %s", task_id, url)

    # 1) 取共享 Kali 沙箱（与 Worker / API 同一容器单例，不新建）
    from pobi_v2 import sandbox_bootstrap

    manager = sandbox_bootstrap._get_manager()
    sandbox = manager.get_or_create_shared_kali()

    # 2) 在 Kali 内执行 curl（同步 Docker SDK，用 to_thread 包裹避免阻塞事件循环）
    cmd = _build_curl_command(url)
    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(sandbox.execute_command, cmd, False, 40),
            timeout=PROBE_HARD_TIMEOUT,
        )
    except asyncio.TimeoutError:
        raise RuntimeError(f"探针在 Kali 内执行 curl 超时（>{PROBE_HARD_TIMEOUT}s）")
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"共享 Kali 执行 curl 失败：{exc}")

    rc = result.get("exit_code")
    stdout = (result.get("stdout") or "").strip()
    stderr = (result.get("stderr") or "").strip()

    # 3) 若 curl 无法解析（如目标需 body 才有反应），退化为带 body 的 GET 取首行
    if rc != 0 or "HTTP_CODE=" not in stdout:
        fallback = (
            f"curl -sS --max-time 30 -w '\\nHTTP_CODE=%{{http_code}}\\n' '{url}' "
            f"| head -c 800"
        )
        try:
            result2 = await asyncio.to_thread(sandbox.execute_command, fallback, False, 40)
            rc = result2.get("exit_code")
            stdout = (result2.get("stdout") or "").strip()
            stderr = (result2.get("stderr") or "").strip()
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"共享 Kali 执行 curl(回退) 失败：{exc}")

    kali_ok = rc == 0 and "HTTP_CODE=" in stdout
    logger.info("[probe %s] Kali curl rc=%s stdout=%r", task_id, rc, stdout[:300])

    # 4) 用 1 次 LLM 调用解读结果，给出一句连通性结论
    summary = await _summarize(url, rc, stdout, stderr, kali_ok, task_id=str(task_id))

    # 5) 组装兼容产出：probe 不产生漏洞 findings，仅记录连通结论
    return {
        "summary": summary,
        "confidence": 0.9 if kali_ok else 0.3,
        "structured_report": {
            "summary": summary,
            "kali": {"exit_code": rc, "stdout": stdout, "stderr": stderr},
            "target_reachable": kali_ok,
        },
        "findings": [],
    }


async def _summarize(url: str, rc: Any, stdout: str, stderr: str, kali_ok: bool, *, task_id: str) -> str:
    """用单次 LLM 调用把 curl 结果转成一句中文连通性结论。

    同时通过 ``EventHooks.emit_llm_response`` 把 LLM usage 累计到会话级计数器
    （与主路径 DeadEndAgent 一致），否则 probe 任务的 ``task.prompt_tokens`` 等
    字段永远为 0，导致「任务 Token 明细」表里 probe 行显示 0 0 0。
    """
    from pobi_v2.llm.client import chat
    from pobi_v2.llm.types import LLMMessage

    verdict = "可达" if kali_ok else "不可达"
    prompt = (
        f"你是一个网络连通性检查助手。以下是在共享 Kali 沙箱中对授权目标 {url} 执行 "
        f"curl 探测的原始结果：\n"
        f"exit_code={rc}\nstdout={stdout!r}\nstderr={stderr!r}\n"
        f"请只用一句中文说明：目标当前{verdict}，并给出关键证据（如 HTTP 状态码）。"
        f"不要展开渗透建议，不超过 60 字。"
    )
    try:
        resp = await asyncio.wait_for(
            chat([LLMMessage(role="user", content=prompt)], temperature=0.0, max_tokens=80),
            timeout=30,
        )
        # 累计会话级 token 用量，让 probe 任务也能在「任务 Token 明细」表里
        # 正确显示 prompt/completion/total_tokens。
        try:
            from pobi_v2.engine.event_bus import PobiV2EventHooks
            PobiV2EventHooks().emit_llm_response(
                session_id=task_id,
                agent_name="probe",
                response_text=resp.content or "",
                usage=resp.usage,
            )
        except Exception as exc:  # noqa: BLE001 — 累计失败不影响结论
            logger.warning("[probe] 累计 token 失败：%s", exc)

        text = (resp.content or "").strip()
        if text:
            return text
    except Exception as exc:  # noqa: BLE001 — LLM 失败不影响连通结论本身
        logger.warning("[probe] LLM 解读失败，回退到原始结论：%s", exc)

    # LLM 不可用时的兜底结论（仍保证探针能结束）
    code_line = next((l for l in stdout.splitlines() if l.startswith("HTTP_CODE=")), "")
    return f"目标{verdict}（curl exit_code={rc} {code_line}）".strip()
