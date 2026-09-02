"""Kali 沙箱内执行 katana 的命令封装与 sitemap 组合器。

katana 由用户在自建 Kali 镜像中安装打包（本项目不做安装/固化），
本模块仅做「调用假设 + 健康检查」：katana 缺失时 sitemap 阶段降级跳过，
不阻断任务主流程。

执行链路：构造命令 → Kali ``execute_command`` → JSONL stdout →
原始输出落盘（防误过滤复盘）→ ``pobi_agent.tools.sitemap`` 解析/去噪/落库。
"""

from __future__ import annotations

import asyncio
import json
import logging
import shlex
import time
from pathlib import Path
from typing import Any

from pobi_agent.tools.sitemap import parse_katana_jsonl, persist_sitemap

logger = logging.getLogger(__name__)

# sitemap 整体执行超时兜底（katana 默认并发/速率，30 分钟大站点也不应超此界）。
SITEMAP_TIMEOUT: float = 300.0
# katana 健康检查超时。
_KATANA_CHECK_TIMEOUT: float = 30.0

# 无意义静态资源扩展名（与 utils.urls 静态扩展名一致，katana -ef 参数层）。
_KATANA_EXCLUDE_EXT = (
    "png,jpg,jpeg,gif,svg,ico,webp,bmp,avif,tiff,heic,"
    "woff,woff2,ttf,eot,otf,"
    "css,scss,less,map,"
    "pdf,zip,gz,tar,7z,rar,mp4,mp3,webm,ogg,avi,mov,wav,flv"
)

# 登出/退出链接正则：认证态抓取时由 katana -cos 在爬虫层排除（不发起请求），
# 避免爬虫顺着 Logout 链接触发服务端 session 销毁，导致同批次其余请求全 302。
# 注意 -ef（exclude filter）只在输出层过滤、仍会访问该 URL；-cos（crawl-out-scope）
# 才是爬虫层不跟随/不请求，二者语义不同，此处必须用 -cos。
KATANA_LOGOUT_EXCLUDE_RE = r"(?i)(logout|logoff|sign[-_]?out|/exit|deconnexion)"

# 追加到 katana 参数之上的去噪参数（默认参数 -d 3 / -fs rdn / -c 10 / -rl 150 保持默认）。
# 仅使用 katana README 确认存在的 flags：-ef 静态资源过滤、-iqp 忽略 query 变体。
# （README 无 -pcs/-fsu/-filter-page-type，内容相似/相似 URL 过滤由落库层 utils.urls 承担。）
KATANA_EXTRA_ARGS: tuple[str, ...] = (
    "-d", "3",
    "-fs", "rdn",
    "-timeout", "10",
    "-retry", "1",
    "-ef", _KATANA_EXCLUDE_EXT,
    "-iqp",
)


def build_katana_command(
    target: str,
    *,
    cookies: dict[str, str] | None = None,
    extra_headers: dict[str, str] | None = None,
    extra_args: list[str] | None = None,
) -> str:
    """构造 katana 命令串（shell 执行，target 经 shlex 引用防注入）。

    - 输出 ``-j`` JSONL（含 request/response 分字段，供格式化储存）
    - ``-silent -nc -duc``：仅输出结果、无 banner、关闭更新检查（容器内省网）
    - 认证：注入 Cookie / 自定义 header（第 4 点：复用 preauth 会话）
    - 去噪：静态资源扩展名（-ef）、忽略 query 变体（-iqp）；
      相似 URL / 无意义页面由落库层 utils.urls 兜底过滤
    """
    cmd = ["katana", "-u", target, "-j", "-silent", "-nc", "-duc"]
    cmd += list(KATANA_EXTRA_ARGS)
    if cookies:
        cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items())
        cmd += ["-H", f"Cookie: {cookie_str}"]
    if extra_headers:
        for k, v in extra_headers.items():
            cmd += ["-H", f"{k}: {v}"]
    # 认证态抓取：登出链接在爬虫层排除（不发起请求），避免自焚 session。
    if cookies or extra_headers:
        cmd += ["-cos", KATANA_LOGOUT_EXCLUDE_RE]
    if extra_args:
        cmd += list(extra_args)
    return " ".join(shlex.quote(c) for c in cmd)


def _sandbox() -> Any:
    """获取全局共享 Kali 沙箱（延迟导入避免 pobi_agent→pobi_v2 逆向依赖）。"""
    from pobi_v2.sandbox_bootstrap import _get_manager

    return _get_manager().get_or_create_shared_kali()


async def katana_available() -> bool:
    """健康检查：Kali 容器内 katana 可执行（用户自装打包）。"""
    try:
        sandbox = await asyncio.to_thread(_sandbox)
        res = await asyncio.to_thread(
            sandbox.execute_command,
            "katana -version",
            False,
            _KATANA_CHECK_TIMEOUT,
        )
        return res.get("exit_code", 1) == 0
    except Exception as exc:  # noqa: BLE001 - 健康检查失败降级跳过
        logger.warning("[sitemap] katana 健康检查失败（降级跳过）: %s", exc)
        return False


async def run_katana(
    target: str,
    *,
    cookies: dict[str, str] | None = None,
    extra_headers: dict[str, str] | None = None,
    timeout: float = SITEMAP_TIMEOUT,
) -> dict[str, Any]:
    """在 Kali 沙箱执行 katana，返回 {"ok","stdout","stderr","exit_code","timed_out"}。"""
    command = build_katana_command(target, cookies=cookies, extra_headers=extra_headers)
    logger.info("[sitemap] 执行 katana: %s", command)
    try:
        sandbox = await asyncio.to_thread(_sandbox)
        res = await asyncio.to_thread(
            sandbox.execute_command, command, False, timeout
        )
        return {
            "ok": res.get("exit_code", 1) == 0,
            "stdout": res.get("stdout", "") or "",
            "stderr": res.get("stderr", "") or "",
            "exit_code": res.get("exit_code"),
            "timed_out": bool(res.get("timed_out", False)),
        }
    except Exception as exc:  # noqa: BLE001 - 执行失败返回错误结构
        logger.warning("[sitemap] katana 执行失败: %s", exc)
        return {"ok": False, "stdout": "", "stderr": "", "exit_code": -1, "timed_out": False, "error": str(exc)}


async def run_sitemap(
    *,
    task_id,
    target_url: str,
    task_root: Path,
    hooks: Any,
    host_hint: str = "",
    cookies: dict[str, str] | None = None,
    extra_headers: dict[str, str] | None = None,
    auth_mode: str = "external_only",
) -> dict[str, Any]:
    """执行 sitemap 构建（katana）并落库、推送实时流。

    失败不 raise，返回 error/skipped 字段供上层记录；katana 未安装时
    status="skipped"（不阻断任务主流程）。

    Returns:
        {"phase","tool","status","target","host","auth_mode","summary","error?"}
        summary: {"total","kept","dropped","endpoints","transactions","auth_required"}
    """
    task_key = str(task_id)
    emit = hooks if hooks is not None else None
    start = time.monotonic()
    try:
        if emit is not None:
            emit.emit_phase_changed(
                task_key, "pre_recon", detail="前置侦查：站点地图构建（katana）"
            )
            emit.emit_tool_call_start(
                session_id=task_key,
                agent_name="pre_recon",
                tool_name="sitemap:katana",
                args=json.dumps({"target": target_url}, ensure_ascii=False),
            )

        if not await katana_available():
            logger.warning("[sitemap %s] katana 未安装（Kali 镜像内），跳过 sitemap", task_key)
            _emit_end(emit, task_key, False, "katana 未安装，跳过站点地图")
            return {
                "phase": "pre_recon", "tool": "sitemap:katana", "status": "skipped",
                "target": target_url, "host": host_hint or _host_of(target_url),
                "auth_mode": auth_mode,
                "error": "katana 未安装（需在 Kali 镜像内预装并打包）",
            }

        result = await asyncio.wait_for(
            run_katana(target_url, cookies=cookies, extra_headers=extra_headers),
            timeout=SITEMAP_TIMEOUT,
        )
        raw = result["stdout"]
        if not result["ok"] or not raw.strip():
            msg = result.get("stderr") or result.get("error") or "katana 无输出或执行失败"
            _emit_end(emit, task_key, False, msg[:500])
            return {
                "phase": "pre_recon", "tool": "sitemap:katana", "status": "error",
                "target": target_url, "host": host_hint or _host_of(target_url),
                "auth_mode": auth_mode, "error": msg[:500],
            }

        # 原始 JSONL 落盘（防误过滤复盘 + 站点地图 raw 证据）。
        sitemap_dir = task_root / "sitemap"
        sitemap_dir.mkdir(parents=True, exist_ok=True)
        raw_path = sitemap_dir / "katana_raw.jsonl"
        try:
            raw_path.write_text(raw, encoding="utf-8")
        except Exception as exc:  # noqa: BLE001 - 原始输出落盘失败不阻断
            logger.warning("[sitemap] 原始 JSONL 落盘失败: %s", exc)

        entries = parse_katana_jsonl(raw)
        task_root.mkdir(parents=True, exist_ok=True)
        from pobi_agent.recon import ReconStore

        store = ReconStore.for_task(task_key, str(task_root))
        try:
            stats = persist_sitemap(
                store,
                task_key,
                entries,
                source="sitemap:katana",
                auth_used=bool(cookies),
                host_hint=host_hint,
            )
        finally:
            store.close()

        duration_ms = int((time.monotonic() - start) * 1000)
        _emit_end(emit, task_key, True, json.dumps(stats, ensure_ascii=False), duration_ms)
        return {
            "phase": "pre_recon", "tool": "sitemap:katana", "status": "completed",
            "target": target_url, "host": host_hint or _host_of(target_url),
            "auth_mode": auth_mode, "summary": stats,
        }
    except asyncio.TimeoutError:
        logger.warning("[sitemap %s] 执行超时（%ss）", task_key, SITEMAP_TIMEOUT)
        return {
            "phase": "pre_recon", "tool": "sitemap:katana", "status": "error",
            "target": target_url, "host": host_hint or _host_of(target_url),
            "auth_mode": auth_mode, "error": f"sitemap 执行超时（{int(SITEMAP_TIMEOUT)}s）",
        }
    except Exception as exc:  # noqa: BLE001 - sitemap 失败不阻断任务主流程
        logger.exception("[sitemap %s] 执行失败", task_key)
        return {
            "phase": "pre_recon", "tool": "sitemap:katana", "status": "error",
            "target": target_url, "host": host_hint or _host_of(target_url),
            "auth_mode": auth_mode, "error": str(exc),
        }


def _host_of(url: str) -> str:
    from urllib.parse import urlparse

    return urlparse(url).netloc or url


def _emit_end(emit, task_key: str, success: bool, result: str, duration_ms: int = 0) -> None:
    if emit is None:
        return
    try:
        emit.emit_tool_call_end(
            session_id=task_key,
            agent_name="pre_recon",
            tool_name="sitemap:katana",
            success=success,
            result=result,
            duration_ms=duration_ms,
        )
    except Exception:  # noqa: BLE001 - 事件推送失败不阻断
        pass
