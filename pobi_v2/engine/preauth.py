"""认证前置服务（PreAuthService）。

任务创建阶段完成登录，产出与原生 AuthAgent 完全一致的 AuthContext 三件套
（{profile}.json + {profile}.playwright.json + index.json），供 L0 认证后爬取
与 exploitation 阶段复用。

两个分支：
- auto   ：LLM 驱动前置认证（``PreAuthAgent`` + ``observe_login_surface``），由模型
  观察登录面后决策 form/json/http/oauth 形态并调用 ``authenticate`` 落盘会话。
- manual ：（已禁用，2026-09-01 搁置，见 .ai/roadmap.md MFA 演进计划）临时 ``BrowserSession`` + 截图轮询远程控制。

设计约束（与原生认证体系对齐）：
- 落盘前必须 ``set_task_root(task_root)``，使 AuthContextHandler 定位到
  ``tasks/<task_id>/agent/auth_context``。
- 会话写入专用 profile（默认 ``preauth``），与原生 AuthAgent 自有 profile（如
  target_session）文件级隔离，天然不被覆盖。
- 认证结果写入 recon_facts（category=authentication），经 L0/L1 上下文注入下游。
"""
from __future__ import annotations

import asyncio
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from pobi_agent.auth_resolver.auth_resolver import (
    AuthType,
    CredentialsStore,
)
from pobi_agent.logging import logger
from pobi_agent.recon.store import ReconStore
from pobi_agent.storage_context import clear_task_root, set_task_root
from pobi_agent.tools.browser.authenticate import authenticate_service

# [DISABLED 2026-09-01] 仅手动登录分支使用；手动分支搁置后无引用
# from pobi_agent.tools.browser.browser import BrowserSession, ClickStep, FillStep, PressStep

# [DISABLED 2026-09-01] 手动会话超时（秒），手动分支搁置后无引用
# MANUAL_SESSION_TTL_S = 10 * 60

# MFA / 二次验证启发式关键词：自动认证失败时用于归类 auth_status=mfa
_MFA_HINTS = ("mfa", "2fa", "two-factor", "two factor", "otp", "totp", "验证码", "二次验证", "短信", "authenticator")

# LLM 前置认证（PreAuthAgent）整体超时（秒）：观察登录面 + LLM 决策 + 登录 + 落盘
PREAUTH_AGENT_TIMEOUT_S = 180


class PreAuthError(Exception):
    """认证前置失败（可读原因存于 message）。"""


def _auth_status_from_result(result: dict[str, Any]) -> tuple[str, str | None]:
    """把 authenticate_service 返回结果归类为 auth_status。

    返回 (status, error_message)；status ∈ success / mfa / failed / aborted。
    """
    if result.get("success"):
        return "success", None
    if result.get("status") == "aborted":
        return "failed", str(result.get("error") or "认证熔断：连续失败已达上限")
    # MFA 启发式：失败时页面/错误信息含验证码类特征 → 归类为 mfa，引导人工分支
    blob = " ".join(
        str(v) for v in result.values() if isinstance(v, (str, int, float))
    ).lower()
    if any(h in blob for h in _MFA_HINTS):
        return "mfa", str(result.get("error") or "检测到二次验证（MFA/验证码），需人工登录")
    return "failed", str(result.get("error") or "认证失败")


def _status_from_agent_result(result: Any) -> tuple[str, str | None]:
    """把 PreAuthAgent 运行结果归类为 auth_status。

    优先证据是 AuthContext 落盘文件（由 run_auto_auth 先检查）；走到这里时
    文件未落盘，只能依据 LLM 输出归类：MFA 标记 → mfa，其余 → failed。
    """
    out = getattr(result, "output", None)
    summary = ""
    if out is not None:
        summary = " ".join(
            str(getattr(out, field, "") or "")
            for field in ("detailed_summary", "thoughts", "proofs")
        )
    low = summary.lower()
    if "requires_interactive_auth" in low or any(h in low for h in _MFA_HINTS):
        return "mfa", "检测到二次验证（MFA/验证码），需人工登录"
    error = str(getattr(result, "error", None) or "") or summary.strip() or "认证未成功"
    return "failed", error[:500]


def _write_auth_facts(
    *,
    task_id: str,
    task_root: Path,
    target: str,
    profile: str,
    source: str,
    auth_status: str,
    username: str | None,
    error: str | None,
) -> None:
    """把认证结果写入 recon_facts（category=authentication），供 L0/L1 注入下游。"""
    store = ReconStore.for_task(task_id, str(task_root))
    store.upsert_fact(
        task_id,
        "authentication",
        "auth_profile",
        profile,
        confidence=0.9,
        source=source,
        details={"auth_status": auth_status, "target": target},
    )
    store.upsert_fact(
        task_id,
        "authentication",
        "auth_mode",
        source,
        confidence=0.9,
        source=source,
    )
    store.upsert_fact(
        task_id,
        "authentication",
        "auth_status",
        auth_status,
        confidence=0.9,
        source=source,
        details={"error": error} if error else None,
    )


def save_task_credentials(
    *,
    task_root: Path,
    target: str,
    username: str,
    password: str,
    login_url: str | None = None,
) -> Path:
    """把任务凭据写入任务目录钱包（``tasks/<task_id>/reusable_credentials.json``）。

    凭据是不可复用资产：随任务目录存续、任务结束即作废，不落任何数据库。
    需在 task_root 注入下调用（内部 set_task_root，写后复位）。
    """
    token = set_task_root(task_root)
    try:
        return CredentialsStore.save_credentials(
            target,
            "preauth",
            username=username,
            password=password,
            login_url=login_url,
        )
    finally:
        clear_task_root(token)


async def verify_credentials(
    *,
    target: str,
    login_url: str | None,
    username: str,
    password: str,
    auth_flow: str = "form",
) -> dict[str, Any]:
    """创建任务前预检凭据：真实登录一次并返回是否有效（不落盘会话）。

    用于任务创建阶段"主动探测验证"——用户配置账号密码后先验证，凭据错误则
    不允许发放任务。与 ``run_auto_auth`` 的区别：
    - 使用一次性临时目录 + 唯一临时 profile（``verify_<uuid>``），不触碰任务
      auth_context 目录，也不污染 ``_auth_fail_counter`` 熔断计数；
    - 验证结束后清理临时目录，不保留任何会话产物。

    返回结构化结果：{ok, status, valid, message, error, took_ms}。
    - status=success → valid=True，凭据有效；
    - status=mfa     → valid=False，message 提示需人工登录（MFA/验证码）；
    - status=failed  → valid=False，凭据错误（账号/密码/登录形态不支持）；
    - status=aborted → valid=False，认证框架无法处理该登录形态，建议手动分支；
    - status=error   → valid=False，验证过程异常。
    """
    tmp_root = Path(tempfile.mkdtemp(prefix="pobi_verify_"))
    verify_profile = f"verify_{uuid4().hex[:8]}"
    started = time.monotonic()
    try:
        token = set_task_root(tmp_root)
        try:
            result = await authenticate_service(
                target=target,
                agent_id=None,
                session_id=uuid4(),
                auth_url=login_url,
                profile=verify_profile,
                auth_flow=auth_flow,
                auth_type=AuthType.SESSION_COOKIE.value,
                username=username,
                password=password,
                headless=True,
                auto_submit=True,
                navigation_timeout_ms=25_000,
                action_timeout_ms=12_000,
            )
        finally:
            clear_task_root(token)
    except Exception as exc:  # noqa: BLE001 — 预检异常必须归类为验证失败，不抛给调用链
        return {
            "ok": False,
            "status": "error",
            "valid": False,
            "message": f"验证过程异常: {exc}",
            "error": str(exc),
            "took_ms": int((time.monotonic() - started) * 1000),
        }
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)

    took_ms = int((time.monotonic() - started) * 1000)
    if result.get("success"):
        return {
            "ok": True,
            "status": "success",
            "valid": True,
            "message": "登录成功，凭据有效",
            "took_ms": took_ms,
        }
    if result.get("aborted") or result.get("status") == "aborted":
        return {
            "ok": True,
            "status": "aborted",
            "valid": False,
            "message": "认证框架无法处理该登录形态（如动态 CSRF/验证码），建议改用「手动登录」",
            "error": str(result.get("error") or ""),
            "took_ms": took_ms,
        }
    status, error = _auth_status_from_result(result)
    if status == "mfa":
        return {
            "ok": True,
            "status": "mfa",
            "valid": False,
            "message": "检测到二次验证（MFA/验证码），创建任务后将走「手动登录」",
            "error": error,
            "took_ms": took_ms,
        }
    return {
        "ok": True,
        "status": "failed",
        "valid": False,
        "message": "账号或密码错误，请检查后重试",
        "error": error,
        "took_ms": took_ms,
    }


async def run_auto_auth(
    *,
    task_id: str,
    task_root: Path,
    target: str,
    login_url: str | None,
    username: str,
    password: str,
    profile: str = "preauth",
    auth_flow: str = "form",
) -> dict[str, Any]:
    """自动分支：LLM 驱动前置认证（PreAuthAgent）完成登录并持久化会话。

    PreAuthAgent 先 ``observe_login_surface`` 观察真实登录面，再由 LLM 决策
    auth_flow（form/json/http/oauth）与步骤，调用 ``authenticate`` 落盘
    ``preauth`` AuthContext。凭据经任务钱包（profile=preauth）解析，
    明文不进入 LLM 上下文。

    返回结构化结果：{status, profile, storage_path, error, detail}。
    调用方负责更新任务 auth_status 与 auth_error。
    """
    # 凭据先落任务钱包（幂等）：LLM 认证走 wallet 解析，明文不进上下文。
    # 落钱包失败不阻断认证（仅导致 wallet 无凭据，authenticate 会失败归类）。
    try:
        save_task_credentials(
            task_root=task_root,
            target=target,
            username=username,
            password=password,
            login_url=login_url,
        )
    except Exception as exc:  # noqa: BLE001 — 凭据落文件失败不阻断 LLM 认证流程
        logger.warning(
            "[PREAUTH] 任务凭据落钱包失败 | task_id=%s | error=%s", task_id, exc
        )

    token = set_task_root(task_root)
    try:
        from pobi_agent.agents.generic_agents.preauth_agent import PreAuthAgent
        from pobi_agent.utils.structures import PreAuthDeps
        from pobi_v2.llm import get_model_spec

        model = get_model_spec()
        session_id = uuid4()
        deps = PreAuthDeps(
            target=target,
            agent_id=uuid4(),
            session_id=session_id,
            login_url=login_url or target,
            task_root=str(task_root),
            task_id=task_id,
        )
        agent = PreAuthAgent(
            model=model,
            deps_type=PreAuthDeps,
            target_information=target,
            requires_approval=False,
            phase="preauth",
        )
        prompt = (
            f"任务创建阶段前置认证：使用已配置凭据（wallet profile={profile}）"
            f"登录目标并落盘认证会话。\n目标：{target}\n"
            f"登录地址：{login_url or target}\n"
            "完成后报告使用的 auth_flow、匹配的 success 信号；"
            "若遇 MFA/验证码，在摘要中标记 requires_interactive_auth=true。"
        )
        result = await asyncio.wait_for(
            agent.run(
                prompt=prompt,
                deps=deps,
                message_history=[],
                usage=None,
                usage_limits=None,
            ),
            timeout=PREAUTH_AGENT_TIMEOUT_S,
        )
    except TimeoutError:  # 前置认证超时降级为 failed
        status, error = "failed", f"前置认证超时（超过 {PREAUTH_AGENT_TIMEOUT_S} 秒）"
        _write_auth_facts(
            task_id=task_id,
            task_root=task_root,
            target=target,
            profile=profile,
            source="preauth_auto",
            auth_status=status,
            username=username,
            error=error,
        )
        logger.warning(
            "[PREAUTH] 前置认证超时 | task_id=%s | profile=%s | error=%s", task_id, profile, error
        )
        return {"status": status, "profile": profile, "storage_path": None, "error": error}
    except Exception as exc:  # noqa: BLE001 — 认证异常必须归类为失败并降级，不抛给调用链
        status, error = "failed", f"前置认证异常: {exc}"
        _write_auth_facts(
            task_id=task_id,
            task_root=task_root,
            target=target,
            profile=profile,
            source="preauth_auto",
            auth_status=status,
            username=username,
            error=str(exc),
        )
        logger.warning(
            "[PREAUTH] 前置认证异常 | task_id=%s | profile=%s | error=%s", task_id, profile, error
        )
        return {"status": status, "profile": profile, "storage_path": None, "error": str(exc)}
    finally:
        clear_task_root(token)

    # 判定优先看落盘证据（authenticate 成功必然写 {profile}.playwright.json）。
    storage_path = Path(task_root) / "agent" / "auth_context" / f"{profile}.playwright.json"
    if storage_path.exists():
        status, error = "success", None
    else:
        status, error = _status_from_agent_result(result)
    _write_auth_facts(
        task_id=task_id,
        task_root=task_root,
        target=target,
        profile=profile,
        source="preauth_auto",
        auth_status=status,
        username=username,
        error=error,
    )
    if status == "success":
        logger.info(
            "[PREAUTH] LLM 前置认证成功 | task_id=%s | profile=%s | username=%s | "
            "storage_path=%s | auth_context_dir=%s",
            task_id,
            profile,
            username,
            str(storage_path),
            str(storage_path.parent),
        )
    else:
        logger.warning(
            "[PREAUTH] LLM 前置认证失败 | task_id=%s | profile=%s | username=%s | status=%s | error=%s",
            task_id,
            profile,
            username,
            status,
            error,
        )
    detail: dict[str, Any] = {}
    out = getattr(result, "output", None)
    if out is not None:
        detail = {
            "agent_summary": str(getattr(out, "detailed_summary", "") or "")[:1000],
            "confidence_score": getattr(out, "confidence_score", None),
        }
    elif getattr(result, "error", None):
        detail = {"agent_error": str(result.error)[:1000]}
    return {
        "status": status,
        "profile": profile,
        "storage_path": str(storage_path) if status == "success" else None,
        "error": error,
        "detail": detail,
    }


# ==================== [DISABLED 2026-09-01] 手动登录（MFA 人工流程）已搁置，见 .ai/roadmap.md MFA 演进计划 ====================
# class ManualAuthSession:
#     """手动分支：一次性 BrowserSession，截图轮询展示 + 指令驱动交互 + 会话捕获。

#     生命周期由调用方通过 start / action / capture / abort 驱动；进程内按 task_id
#     注册，超时自动销毁。
#     """

#     def __init__(
#         self,
#         *,
#         task_id: str,
#         task_root: Path,
#         target: str,
#         login_url: str,
#         profile: str = "preauth",
#     ) -> None:
#         self.task_id = task_id
#         self.task_root = task_root
#         self.target = target
#         self.login_url = login_url
#         self.profile = profile
#         self.browser = BrowserSession(headless=True, verify_ssl=False)
#         self._started_mono = time.monotonic()
#         self.started = False

#     # -- 生命周期 ----------------------------------------------------------

#     async def start(self) -> None:
#         """启动浏览器并导航到登录页。"""
#         await self.browser.start()
#         self.started = True
#         self._started_mono = time.monotonic()
#         if self.login_url:
#             await self.browser.goto(self.login_url, timeout_ms=30_000)

#     async def stop(self) -> None:
#         """销毁浏览器实例。"""
#         if self.browser.started:
#             await self.browser.stop()
#         self.started = False

#     def expired(self) -> bool:
#         return time.monotonic() - self._started_mono > MANUAL_SESSION_TTL_S

#     # -- 远程控制 ----------------------------------------------------------

#     async def snapshot(self) -> dict[str, Any]:
#         """返回当前页面截图（base64）与地址/标题，供前端轮询渲染。"""
#         if not self.browser.started:
#             return {"ok": False, "error": "浏览器未启动"}
#         b64 = await self.browser.screenshot(as_base64=True)
#         url = await self.browser.get_url()
#         title = await self.browser.get_title()
#         return {
#             "ok": True,
#             "screenshot_base64": b64 or "",
#             "url": url,
#             "title": title,
#         }

#     async def navigate(self, url: str) -> dict[str, Any]:
#         await self.browser.goto(url, timeout_ms=30_000)
#         return {"ok": True}

#     async def click(self, selector: str) -> dict[str, Any]:
#         await self.browser.run_steps([ClickStep(selector)], context={}, timeout_ms=15_000)
#         return {"ok": True}

#     async def fill(self, selector: str, text: str) -> dict[str, Any]:
#         key = "__preauth_fill"
#         await self.browser.run_steps(
#             [FillStep(selector, key)], context={key: text}, timeout_ms=15_000
#         )
#         return {"ok": True}

#     async def press(self, selector: str, key: str = "Enter") -> dict[str, Any]:
#         await self.browser.run_steps([PressStep(selector, key)], context={}, timeout_ms=15_000)
#         return {"ok": True}

#     async def eval(self, script: str) -> dict[str, Any]:
#         value = await self.browser.execute_script(script)
#         return {"ok": True, "result": value}

#     async def wait(self, ms: int) -> dict[str, Any]:
#         await asyncio.sleep(max(0, min(ms, 30_000)) / 1000)
#         return {"ok": True}

#     async def action(self, action_type: str, **params: Any) -> dict[str, Any]:
#         """统一指令入口：navigate / click / fill / press / eval / wait。"""
#         if self.expired():
#             await self.stop()
#             return {"ok": False, "error": "会话超时（10 分钟未操作），已自动销毁，请重新启动"}
#         if action_type == "navigate":
#             return await self.navigate(params.get("url", ""))
#         if action_type == "click":
#             return await self.click(params.get("selector", ""))
#         if action_type == "fill":
#             return await self.fill(params.get("selector", ""), params.get("text", ""))
#         if action_type == "press":
#             return await self.press(params.get("selector", ""), params.get("key", "Enter"))
#         if action_type == "eval":
#             return await self.eval(params.get("script", ""))
#         if action_type == "wait":
#             return await self.wait(int(params.get("ms", 1000)))
#         return {"ok": False, "error": f"未知指令: {action_type}"}

#     async def capture(self) -> dict[str, Any]:
#         """导出浏览器状态并落盘为 AuthContext 三件套（与原生格式零差异）。"""
#         if not self.browser.started:
#             return {"ok": False, "error": "浏览器未启动"}
#         state = await self.browser.export_state()
#         token = set_task_root(self.task_root)
#         try:
#             handler = AuthContextHandler(self.target, None, uuid4())
#             agent_id = str(uuid4())
#             auth_context = auth_context_from_browser_state(
#                 profile=self.profile,
#                 target=self.target,
#                 agent_id=agent_id,
#                 session_id=str(self.task_id),
#                 state=state,
#                 auth_url=self.login_url,
#                 auth_flow=AuthFlow.FORM.value,
#                 auth_type=AuthType.SESSION_COOKIE.value,
#                 extra_metadata={"source": "preauth_manual"},
#             )
#             handler.save_context(self.profile, auth_context)
#             write_playwright_storage_state(
#                 auth_context,
#                 handler.playwright_storage_path(self.profile),
#                 target=self.target,
#             )
#         finally:
#             clear_task_root(token)

#         _write_auth_facts(
#             task_id=self.task_id,
#             task_root=self.task_root,
#             target=self.target,
#             profile=self.profile,
#             source="preauth_manual",
#             auth_status="success",
#             username=None,
#             error=None,
#         )
#         return {
#             "ok": True,
#             "profile": self.profile,
#             "storage_path": str(handler.playwright_storage_path(self.profile)),
#             "cookies_count": len(state.get("cookies") or []),
#         }


# 进程内手动会话注册表（按 task_id 隔离；api 单 worker 场景下有效）
# _MANUAL_SESSIONS: dict[str, ManualAuthSession] = {}


# def get_manual_session(task_id: str) -> ManualAuthSession | None:
#     return _MANUAL_SESSIONS.get(task_id)


# def register_manual_session(task_id: str, session: ManualAuthSession) -> None:
#     _MANUAL_SESSIONS[task_id] = session


# def unregister_manual_session(task_id: str) -> None:
#     _MANUAL_SESSIONS.pop(task_id, None)
