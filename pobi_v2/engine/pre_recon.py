"""前置侦查通道：任务启动后、智能体侦查前自动执行基础信息测绘。

职责（平台层自动，不依赖 LLM 决策）：
- 指纹识别 + WAF 识别（复用 ``pobi_agent.tools.fingerprint.engine``）
- 站点地图构建（复用 ``pobi_v2.engine.sitemap``，Kali 内执行 katana）
- 认证感知：任务创建阶段 PreAuth 已落盘 ``preauth`` 会话，此处读取并注入
  cookies（登录后补充探测）；有认证任务会带超时等待会话落盘（避免 pre_recon
  抢跑在后台认证完成前导致 sitemap 匿名爬取），无会话则仅外部探测并在结果中提示
- 落库：``recon_fingerprints`` 明细表 + ``recon_facts(technology)`` +
  ``recon_endpoints(tech_stack)`` + ``recon_techniques(fingerprint|host)``
  足迹（指纹），以及 ``recon_http_transactions``/``recon_endpoints``
  + ``recon_techniques(sitemap|host)`` 足迹（站点地图），供 L0/L1 注入下游
- 实时流：进入阶段「前置侦查」+ tool 调用（fingerprint / sitemap:katana）事件推送前端

与 PreAuth 互补：PreAuth 负责认证（recon_facts authentication），本通道
负责目标侧客观测绘（技术栈/WAF/端点基线）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from pobi_agent.auth_resolver import AuthContextHandler
from pobi_agent.recon import ReconStore
from pobi_agent.storage_context import clear_task_root, set_task_root
from pobi_agent.tools.fingerprint.engine import (
    persist_fingerprint,
    run_fingerprint,
)
from pobi_v2.engine.sitemap import run_sitemap

logger = logging.getLogger(__name__)

# 前置侦查整体超时兜底（指纹采集 12s + favicon 8s + sitemap 300s + 余量）
PRE_RECON_TIMEOUT: float = 400.0

# 认证会话 profile（PreAuth 落盘专用）
PREAUTH_PROFILE: str = "preauth"

# 等待 PreAuth 认证会话落盘的超时与轮询间隔（有认证任务由 executor 传入；
# 避免 pre_recon 抢跑在后台认证完成前导致 sitemap 无凭证爬取）。
PREAUTH_WAIT_TIMEOUT: float = 60.0
PREAUTH_WAIT_INTERVAL: float = 1.0


def _resolve_auth(
    task_root: Path,
    target: str,
    auth_profile: str = PREAUTH_PROFILE,
) -> tuple[dict[str, str], dict[str, str], str]:
    """读取 PreAuth 落盘的认证会话（无会话降级仅外部探测）。

    Returns:
        (cookies, extra_headers, auth_mode)：auth_mode 取值
        external_only / authenticated:<profile> / no_auth_context:<profile> /
        auth_error:<msg>。
    """
    try:
        handler = AuthContextHandler(
            target=target,
            agent_id=None,
            session_id=None,
        )
        auth_context = handler.load_context(auth_profile)
        if auth_context is None:
            return {}, {}, f"no_auth_context:{auth_profile}"
        cookies = {
            c.name: c.value
            for c in auth_context.cookies
            if c.name and c.value is not None
        }
        return cookies, dict(auth_context.headers), f"authenticated:{auth_profile}"
    except Exception as exc:  # noqa: BLE001 - 认证解析失败降级外部探测
        logger.warning("[pre_recon] 认证会话解析失败（降级外部探测）: %s", exc)
        return {}, {}, f"auth_error:{exc}"


async def _wait_for_auth_context(
    task_root: Path,
    target: str,
    auth_profile: str,
    timeout: float,
) -> tuple[dict[str, str], dict[str, str], str]:
    """带超时等待 PreAuth 认证会话就绪；超时降级外部探测。

    时序根因：worker 侧 pre_recon 与 api 侧后台 preauth 认证并行执行，pre_recon
    可能抢跑在认证落盘（``tasks/<task_id>/agent/auth_context/preauth.json``）之前，
    导致 sitemap 以匿名方式爬取。此处轮询 ``load_context`` 直到出现
    ``authenticated:<profile>``；超时返回当前 auth_mode 降级（不阻断侦查）。
    """
    deadline = time.monotonic() + timeout
    while True:
        cookies, extra_headers, auth_mode = _resolve_auth(task_root, target, auth_profile)
        if auth_mode.startswith("authenticated"):
            return cookies, extra_headers, auth_mode
        if time.monotonic() >= deadline:
            logger.warning(
                "[pre_recon] 等待 PreAuth 认证会话超时（%ss），降级外部探测: %s",
                timeout, auth_mode,
            )
            return {}, {}, auth_mode
        await asyncio.sleep(PREAUTH_WAIT_INTERVAL)


def _summary(result) -> dict[str, Any]:
    """面向前端实时流的精简结果（完整明细在 recon_fingerprints 表）。"""
    return {
        "target": result.target,
        "status_code": result.status_code,
        "favicon_hash": result.favicon_hash,
        "auth_mode": result.auth_mode,
        "matched_count": result.matched_count,
        "fingerprints": result.fingerprints,
    }


async def run_pre_recon(
    *,
    task_id,
    target_url: str,
    task_root: Path,
    hooks: Any,
    auth_profile: str = PREAUTH_PROFILE,
    host_hint: str = "",
    preauth_wait_timeout: float = 0.0,
) -> dict[str, Any]:
    """执行前置侦查（指纹识别 + WAF 识别）并落库、推送实时流。

    Args:
        task_id: 任务 UUID（recon 库 session 维度）。
        target_url: 授权目标 URL。
        task_root: 任务产物统一根目录（tasks/<task_id>）。
        hooks: 事件钩子（PobiV2EventHooks，推送 phase/tool 事件）。
        auth_profile: 认证会话 profile（默认 preauth，PreAuth 落盘）。
        host_hint: 展示用 host（缺省从 target_url 推导）。
        preauth_wait_timeout: >0 时在认证会话未就绪前带超时等待其落盘（秒），
            超时降级外部探测；0（默认）不等待，立即按当前会话状态执行。

    Returns:
        结构化结果 dict：{phase, tool, status, target, auth_mode, summary,
        fingerprints, error?}。失败不 raise，返回 error 字段供上层记录。
    """
    task_key = str(task_id)
    host = host_hint or (urlparse(target_url).netloc or target_url)
    # 事件钩子可能为 None（测试/降级场景）
    emit = hooks if hooks is not None else None

    start = time.monotonic()
    try:
        if emit is not None:
            emit.emit_phase_changed(
                task_key, "pre_recon", detail="前置侦查：指纹识别与 WAF 识别"
            )
            emit.emit_tool_call_start(
                session_id=task_key,
                agent_name="pre_recon",
                tool_name="fingerprint",
                args=json.dumps({"target": target_url}, ensure_ascii=False),
            )
        logger.info("[pre_recon %s] 开始前置侦查：%s", task_key, target_url)

        # 认证会话读取（需 task_root 注入定位 preauth profile）
        token = set_task_root(task_root)
        try:
            cookies, extra_headers, auth_mode = _resolve_auth(
                task_root, target_url, auth_profile
            )
            # 有认证任务：带超时等待 PreAuth 会话落盘，避免抢跑成匿名爬取
            if preauth_wait_timeout > 0 and not auth_mode.startswith("authenticated"):
                cookies, extra_headers, auth_mode = await _wait_for_auth_context(
                    task_root, target_url, auth_profile, preauth_wait_timeout
                )
            result = await asyncio.wait_for(
                run_fingerprint(target_url, cookies, extra_headers or None, auth_mode),
                timeout=PRE_RECON_TIMEOUT,
            )
        finally:
            clear_task_root(token)

        # 落库（fingerprint 明细 + facts + endpoints + 足迹）
        task_root.mkdir(parents=True, exist_ok=True)
        store = ReconStore.for_task(task_key, str(task_root))
        try:
            persist_fingerprint(store, task_key, result, source="pre_recon")
        finally:
            store.close()

        duration_ms = int((time.monotonic() - start) * 1000)
        summary = _summary(result)
        if emit is not None:
            emit.emit_tool_call_end(
                session_id=task_key,
                agent_name="pre_recon",
                tool_name="fingerprint",
                success=result.status_code is not None,
                result=json.dumps(summary, ensure_ascii=False),
                duration_ms=duration_ms,
            )

        if result.status_code is None:
            return {
                "phase": "pre_recon",
                "tool": "fingerprint",
                "status": "error",
                "target": target_url,
                "host": host,
                "auth_mode": auth_mode,
                "error": "目标不可达（连接失败/超时/SSL 异常），未能完成指纹采集",
                "summary": summary,
            }

        # 站点地图构建（sitemap:katana）——复用指纹阶段解析的认证会话。
        # 失败/katana 未安装时仅降级，不阻断前置侦查整体。
        sitemap_result = await asyncio.wait_for(
            run_sitemap(
                task_id=task_id,
                target_url=target_url,
                task_root=task_root,
                hooks=hooks,
                host_hint=host,
                cookies=cookies,
                extra_headers=extra_headers,
                auth_mode=auth_mode,
            ),
            timeout=PRE_RECON_TIMEOUT,
        )

        # 端点树派生：recon_http_transactions 为单一真源，sitemap/fingerprint 落库
        # 后统一派生 recon_endpoints（避免双写不一致，并收敛 PG 同步唯一键）。
        task_root.mkdir(parents=True, exist_ok=True)
        derive_store = ReconStore.for_task(task_key, str(task_root))
        try:
            await derive_store.derive_endpoints_from_transactions(task_key)
        except Exception as exc:  # noqa: BLE001 - 派生失败不阻断前置侦查
            logger.warning("[pre_recon %s] 端点派生失败（已忽略）: %s", task_key, exc)
        finally:
            derive_store.close()

        return {
            "phase": "pre_recon",
            "tool": "fingerprint",
            "status": "completed",
            "target": target_url,
            "host": host,
            "auth_mode": auth_mode,
            "matched_count": result.matched_count,
            "summary": summary,
            "sitemap": sitemap_result.get("summary") or {},
            "sitemap_status": sitemap_result.get("status"),
            "sitemap_error": sitemap_result.get("error", ""),
        }
    except TimeoutError:
        logger.warning("[pre_recon %s] 前置侦查超时（%ss）", task_key, PRE_RECON_TIMEOUT)
        return {
            "phase": "pre_recon", "tool": "fingerprint", "status": "error",
            "target": target_url, "host": host,
            "error": f"前置侦查超时（{int(PRE_RECON_TIMEOUT)}s）",
        }
    except Exception as exc:  # noqa: BLE001 - 前置侦查失败不阻断任务主流程
        logger.exception("[pre_recon %s] 前置侦查失败", task_key)
        return {
            "phase": "pre_recon", "tool": "fingerprint", "status": "error",
            "target": target_url, "host": host, "error": str(exc),
        }
