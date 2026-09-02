"""指纹识别（引擎封装 / 兼容层）。

指纹识别归属**前置侦查**阶段：由平台层 ``pobi_v2.engine.pre_recon`` 在任务
启动后自动执行并落库，**不再作为 requester 的 agent 工具挂载**（agent 直接
复用 fingerprint 表与 L0/L1 注入的技术结论，无需重复探测）。

本模块保留 ``webapp_fingerprint`` 薄封装（供测试与工具事件复用），核心
采集/匹配/聚合/落库逻辑统一收敛到同包 ``engine.py``，避免两套实现漂移。
"""

from __future__ import annotations

import logging
from urllib.parse import urlparse

from pydantic_ai import RunContext

from pobi_agent.auth_resolver import AuthContextHandler
from pobi_agent.recon import ReconStore
from pobi_agent.storage_context import get_task_root
from pobi_agent.tools.fingerprint import engine
from pobi_agent.tools.tool_wrappers import with_tool_events
from pobi_agent.utils.structures import RequesterDeps

logger = logging.getLogger(__name__)


def _resolve_store(ctx: RunContext[RequesterDeps]) -> ReconStore | None:
    """依据 session_id + task_root 定位本地 RECON 库（落库场景即建表）。"""
    try:
        task_root = get_task_root()
        session_id = str(ctx.deps.session_id)
        if task_root is None:
            return None
        db_path = task_root / f"{session_id}.db"
        return ReconStore(db_path)
    except Exception as exc:  # noqa: BLE001 - 落库失败不阻断主流程
        logger.debug("指纹识别: 定位本地库失败（已忽略）: %s", exc)
        return None


def _resolve_auth(
    ctx: RunContext[RequesterDeps],
    auth_profile: str | None,
    target: str,
) -> tuple[dict[str, str], dict[str, str], str]:
    """解析认证会话（兼容入口，pre_recon 通道内部自行解析）。

    Returns:
        (cookies, extra_headers, auth_mode)：
        auth_mode ∈ {"external_only", "authenticated:<profile>",
        "no_auth_context:<profile>", "auth_error:<msg>"}。
    """
    if not auth_profile:
        return {}, {}, "external_only"
    agent_id = getattr(ctx.deps, "agent_id", None)
    session_id = getattr(ctx.deps, "session_id", None)
    if agent_id is None or session_id is None:
        return {}, {}, f"auth_error:agent_id/session_id 缺失，无法加载 profile {auth_profile!r}"
    try:
        handler = AuthContextHandler(
            target=target,
            agent_id=str(agent_id),
            session_id=str(session_id),
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
        logger.warning("指纹识别: auth_profile 解析失败（降级外部探测）: %s", exc)
        return {}, {}, f"auth_error:{exc}"


@with_tool_events("webapp_fingerprint")
async def webapp_fingerprint(
    ctx: RunContext[RequesterDeps],
    target_host: str,
    auth_profile: str | None = None,
) -> dict[str, object]:
    """（兼容封装）对目标执行指纹识别：服务器 / 后端 / 前端 / CMS + WAF。

    核心逻辑委托 ``engine.run_fingerprint``；本函数仅做 ctx 适配（认证
    解析 / store 定位）与足迹防重。agent 已不再挂载本工具，保留仅作
    测试与显式调用兼容。
    """
    target = target_host.strip() if target_host and target_host.strip() else str(ctx.deps.target)
    if not target:
        return {"error": "target_host 不能为空，请提供目标 URL"}
    host = urlparse(target if "://" in target else f"http://{target}").netloc

    # 认证解析（无凭证 → 仅外部探测）
    cookies, extra_headers, auth_mode = _resolve_auth(ctx, auth_profile, target)

    store = _resolve_store(ctx)
    task_id = str(ctx.deps.session_id)
    if store is not None:
        # 足迹防重：同 host 已完成指纹识别则复用既有结论
        try:
            cached = store.lookup(task_id, tech=f"fingerprint|{host}", limit=5)
            if any(
                r.get("kind") == "technique"
                and f"fingerprint|{host}" in (r.get("name") or "")
                for r in cached
            ):
                facts = store.lookup(task_id, category="technology", limit=100)
                store.close()
                return {
                    "target": host,
                    "cached": True,
                    "auth_mode": auth_mode,
                    "hint": "该主机已完成指纹识别，以上为历史结论（recon_facts category=technology），"
                            "可直接复用，无需重复探测。",
                    "fingerprints": {layer: [] for layer in engine.LAYERS},
                    "known_facts": [f"{f.get('key')}: {f.get('value')}" for f in facts],
                }
        except Exception as exc:  # noqa: BLE001 - 防重查询失败继续探测
            logger.debug("指纹足迹防重查询失败（继续探测）: %s", exc)

    result = await engine.run_fingerprint(target, cookies, extra_headers or None, auth_mode)
    if result.status_code is None:
        if store is not None:
            store.close()
        return {
            "target": host,
            "error": "目标不可达（连接失败/超时/SSL 异常），未能完成指纹采集",
            "auth_mode": auth_mode,
            "hint": "请确认目标可达后再试；若首次使用外部探测，可先复核网络连通性。",
            "fingerprints": {layer: [] for layer in engine.LAYERS},
        }

    # 落库（无库时 no-op）
    if store is not None:
        engine.persist_fingerprint(store, task_id, result, source="webapp_fingerprint")
        store.close()

    auth_hint = (
        f"认证模式：{auth_mode}。"
        if auth_mode.startswith("authenticated")
        else "无可用登录凭证，仅执行外部探测（未登录可探测的技术栈信息已采集）；"
             "如需登录后补充指纹（认证页/后台技术栈），请先完成 PreAuth 或提供 auth_profile。"
    )
    return {
        "target": result.target,
        "cached": False,
        "status_code": result.status_code,
        "auth_mode": result.auth_mode,
        "favicon_hash": result.favicon_hash,
        "fingerprints": result.fingerprints,
        "matched_count": result.matched_count,
        "auth_hint": auth_hint,
        "hint": "指纹结果已落 recon_fingerprints / recon_facts(technology)，"
                "后续侦察可用 recon_lookup(tech=...) 复用。",
    }
