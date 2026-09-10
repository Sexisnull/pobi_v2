"""凭据生命周期回归测试（2026-09-10「凭据消费双通道」改造）。

覆盖：
1. ``resolve_auth_profile`` 哨兵语义（None→default、__anonymous__→匿名、具名原样、
   default 缺失降级）—— 核心风险点是「改造前匿名调用不得回归」；
2. ``read_auth_storage`` 按 ``RequesterDeps`` 解析新路径（此前传字符串 session_key
   走 legacy 分支，恒返回 available=False），以及 ``include_secrets`` 的明文开关；
3. 请求后失效闭环 ``_detect_auth_failure`` / ``_track_auth_failure``：
   单次 401 不上报、连续达阈值 + 曾成功才写 ``auth_invalid`` fact；
4. ``pre_recon`` 的 ``auth_required_detected`` 标记。
"""
from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from pobi_agent.auth_resolver import (
    ANONYMOUS_PROFILE,
    DEFAULT_PROFILE,
    AuthContext,
    AuthContextHandler,
    CookieRecord,
    resolve_auth_profile,
)
from pobi_agent.storage_context import clear_task_root, set_task_root
from pobi_agent.tools.python_interpreter import read_auth_storage
from pobi_agent.utils.structures import RequesterDeps

TARGET = "https://pwn.example:8081"


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def task_root(tmp_path: Path):
    """注入 task_root，使 AuthContextHandler 落 ``tasks/<id>/agent/auth_context``。"""
    token = set_task_root(tmp_path)
    try:
        yield tmp_path
    finally:
        clear_task_root(token)


def _save_context(profile: str, *, cookie_value: str = "s3cret") -> None:
    handler = AuthContextHandler(target=TARGET, agent_id=None, session_id=None)
    handler.save_context(
        profile,
        AuthContext(
            profile=profile,
            cookies=[CookieRecord(name="session", value=cookie_value, domain="pwn.example")],
            headers={"Authorization": f"Bearer {cookie_value}"},
        ),
    )


def _requester_deps() -> RequesterDeps:
    return RequesterDeps(
        embedder_client=None,
        rag=None,
        target=TARGET,
        agent_id=uuid.uuid4(),
        session_id=uuid.uuid4(),
    )


# ---------------------------------------------------------------------------
# 1. resolve_auth_profile 语义
# ---------------------------------------------------------------------------

def test_resolve_none_falls_back_to_anonymous_when_no_default(task_root):
    """无 default 会话时 None 必须降级匿名 —— 保证改造前行为不回归。"""
    assert resolve_auth_profile(TARGET, None) is None
    assert resolve_auth_profile(TARGET, "") is None


def test_resolve_none_becomes_default_when_session_exists(task_root):
    """存在 default 会话时 None 解析为 default（默认复用统一凭据）。"""
    _save_context(DEFAULT_PROFILE)
    assert resolve_auth_profile(TARGET, None) == DEFAULT_PROFILE
    assert resolve_auth_profile(TARGET, "") == DEFAULT_PROFILE


def test_resolve_anonymous_sentinel_is_explicit_anonymous(task_root):
    """__anonymous__ 是显式匿名：即使 default 存在也不复用（对照验证入口）。"""
    _save_context(DEFAULT_PROFILE)
    assert resolve_auth_profile(TARGET, ANONYMOUS_PROFILE) is None


def test_resolve_named_profile_passes_through(task_root):
    """具名 profile 原样返回，不受 default 是否存在影响。"""
    assert resolve_auth_profile(TARGET, "admin") == "admin"
    assert resolve_auth_profile(TARGET, "admin") != DEFAULT_PROFILE


def test_resolve_default_missing_from_disk_degrades(task_root):
    """显式传 default 但磁盘无该会话：降级 None，不抛异常。"""
    assert resolve_auth_profile(TARGET, DEFAULT_PROFILE) is None


def test_resolve_survives_handler_failure(monkeypatch):
    """Handler 构造异常时静默降级匿名，不向调用方抛出。"""
    import pobi_agent.auth_resolver.auth_resolver as mod

    class Boom:
        def __init__(self, *a, **kw):
            raise RuntimeError("disk unavailable")

    monkeypatch.setattr(mod, "AuthContextHandler", Boom)
    assert resolve_auth_profile(TARGET, None) is None


# ---------------------------------------------------------------------------
# 2. read_auth_storage 沙箱明文通道
# ---------------------------------------------------------------------------

def test_read_auth_storage_resolves_new_path_with_secrets(task_root):
    """传 RequesterDeps + include_secrets=True：读到新路径会话且含明文。"""
    _save_context(DEFAULT_PROFILE, cookie_value="abc123")
    out = json.loads(
        asyncio.run(
            read_auth_storage(ctx=_requester_deps(), profile="default", include_secrets=True)
        )
    )
    assert out["profile"] == "default"
    assert out["cookies"][0]["value"] == "abc123"
    assert out["headers"]["Authorization"] == "Bearer abc123"


def test_read_auth_storage_safe_summary_hides_secrets(task_root):
    """include_secrets=False（默认）：只回摘要，不含 cookie 值。"""
    _save_context(DEFAULT_PROFILE, cookie_value="abc123")
    out = json.loads(asyncio.run(read_auth_storage(ctx=_requester_deps())))
    assert "abc123" not in json.dumps(out)


def test_read_auth_storage_legacy_string_does_not_read_new_path(task_root):
    """字符串 ctx 走 legacy 分支：读不到新路径 default.json（回归守卫）。"""
    _save_context(DEFAULT_PROFILE, cookie_value="abc123")
    out = json.loads(asyncio.run(read_auth_storage(ctx="pwn.example:8081")))
    assert out["available"] is False


def test_read_auth_storage_missing_profile_reports_unavailable(task_root):
    """会话不存在：返回 available=False，不伪造凭据。"""
    out = json.loads(
        asyncio.run(read_auth_storage(ctx=_requester_deps(), profile="default"))
    )
    assert out["available"] is False


def test_read_auth_storage_deps_without_identity_degrades(task_root):
    """deps 缺 target/session_id：明确回报，不抛异常。"""
    out = json.loads(
        asyncio.run(read_auth_storage(ctx=SimpleNamespace(target=None, session_id=None)))
    )
    assert out["available"] is False


# ---------------------------------------------------------------------------
# 3. 请求后失效闭环
# ---------------------------------------------------------------------------

def _response_bytes(status: int, location: str = "") -> bytes:
    head = f"HTTP/1.1 {status} X\r\n"
    if location:
        head += f"Location: {location}\r\n"
    return (head + "\r\n").encode()


def test_detect_auth_failure_flags_401_and_login_redirect():
    from pobi_agent.tools.browser_automation import _detect_auth_failure

    assert _detect_auth_failure([_response_bytes(401)]) is True
    assert _detect_auth_failure([_response_bytes(403)]) is True
    assert _detect_auth_failure([_response_bytes(302, "/login")]) is True
    assert _detect_auth_failure([_response_bytes(200)]) is False
    assert _detect_auth_failure([_response_bytes(302, "/dashboard")]) is False
    assert _detect_auth_failure([]) is False


def test_track_auth_failure_single_401_does_not_report(task_root):
    """单次 401 不得上报 auth_invalid（可能是越权测试的预期响应）。"""
    from pobi_agent.tools.browser_automation import _track_auth_failure

    _save_context(DEFAULT_PROFILE)
    facts: list[dict] = []
    ctx = SimpleNamespace(
        deps=SimpleNamespace(
            target=TARGET,
            agent_id=None,
            session_id=None,
            context=SimpleNamespace(add_discovered_fact=lambda **kw: facts.append(kw)),
        )
    )
    _track_auth_failure(ctx, DEFAULT_PROFILE, failed=True)
    assert facts == []
    stored = AuthContextHandler(TARGET, None, None).load_context(DEFAULT_PROFILE)
    assert stored.metadata["consecutive_auth_failures"] == 1


def test_track_auth_failure_requires_success_baseline(task_root):
    """连续失败但从未成功过：不满足阈值前提，不上报。"""
    from pobi_agent.tools.browser_automation import _track_auth_failure

    _save_context(DEFAULT_PROFILE)
    facts: list[dict] = []
    ctx = SimpleNamespace(
        deps=SimpleNamespace(
            target=TARGET,
            agent_id=None,
            session_id=None,
            context=SimpleNamespace(add_discovered_fact=lambda **kw: facts.append(kw)),
        )
    )
    for _ in range(5):
        _track_auth_failure(ctx, DEFAULT_PROFILE, failed=True)
    assert facts == []


def test_track_auth_failure_reports_after_success_then_threshold(task_root):
    """先成功建立基线，再连续 3 次失败：上报一次且不重复上报。"""
    from pobi_agent.tools.browser_automation import (
        AUTH_FAILURE_THRESHOLD,
        _track_auth_failure,
    )

    _save_context(DEFAULT_PROFILE)
    facts: list[dict] = []
    ctx = SimpleNamespace(
        deps=SimpleNamespace(
            target=TARGET,
            agent_id=None,
            session_id=None,
            context=SimpleNamespace(add_discovered_fact=lambda **kw: facts.append(kw)),
        )
    )
    _track_auth_failure(ctx, DEFAULT_PROFILE, failed=False)  # 建立成功基线
    for _ in range(AUTH_FAILURE_THRESHOLD - 1):
        _track_auth_failure(ctx, DEFAULT_PROFILE, failed=True)
    assert facts == [], "未达阈值不应上报"

    _track_auth_failure(ctx, DEFAULT_PROFILE, failed=True)
    assert len(facts) == 1
    assert facts[0]["category"] == "authentication"
    assert facts[0]["key"] == f"auth_invalid:{DEFAULT_PROFILE}"
    assert facts[0]["details"]["consecutive_failures"] == AUTH_FAILURE_THRESHOLD

    # 继续失败：已上报过，不重复写
    _track_auth_failure(ctx, DEFAULT_PROFILE, failed=True)
    assert len(facts) == 1


def test_track_auth_failure_success_resets_counter(task_root):
    """成功响应清零连续计数（穿插成功不算连续失效）。"""
    from pobi_agent.tools.browser_automation import _track_auth_failure

    _save_context(DEFAULT_PROFILE)
    ctx = SimpleNamespace(
        deps=SimpleNamespace(
            target=TARGET,
            agent_id=None,
            session_id=None,
            context=SimpleNamespace(add_discovered_fact=lambda **kw: None),
        )
    )
    for _ in range(5):
        _track_auth_failure(ctx, DEFAULT_PROFILE, failed=True)
    _track_auth_failure(ctx, DEFAULT_PROFILE, failed=False)
    stored = AuthContextHandler(TARGET, None, None).load_context(DEFAULT_PROFILE)
    assert stored.metadata["consecutive_auth_failures"] == 0


def test_track_auth_failure_never_breaks_request_on_error(task_root, monkeypatch):
    """计数写盘异常不得向上抛出（不得阻断请求主流程）。"""
    import pobi_agent.tools.browser_automation as mod

    def boom(*a, **kw):
        raise RuntimeError("disk error")

    monkeypatch.setattr(mod, "AuthContextHandler", boom)
    ctx = SimpleNamespace(
        deps=SimpleNamespace(
            target=TARGET,
            agent_id=None,
            session_id=None,
            context=SimpleNamespace(add_discovered_fact=lambda **kw: None),
        )
    )
    mod._track_auth_failure(ctx, DEFAULT_PROFILE, failed=True)  # 不应抛异常


# ---------------------------------------------------------------------------
# 4. pre_recon：匿名探测需登录标记
# ---------------------------------------------------------------------------

def test_mark_auth_required_skipped_when_authenticated(task_root, monkeypatch):
    """已有认证会话：不写 auth_required_detected（不是盲区）。"""
    from pobi_agent.recon import ReconStore
    from pobi_v2.engine import pre_recon

    store = ReconStore.for_task("t-auth", str(task_root))
    try:
        assert pre_recon._mark_auth_required_if_needed(
            store, "t-auth", "authenticated:preauth"
        ) is False
    finally:
        store.close()


def test_mark_auth_required_when_anon_and_protected(task_root):
    """匿名 + 观测到需认证端点：写入 recon_facts 并返回 True。"""
    from pobi_agent.recon import ReconStore
    from pobi_v2.engine import pre_recon

    store = ReconStore.for_task("t-anon", str(task_root))
    try:
        store.ensure_session("t-anon", target=TARGET)
        store.insert_http_transaction(
            "t-anon",
            source="agent:requester",
            method="GET",
            url=f"{TARGET}/admin",
            status_code=401,
            auth_required=True,
        )
        assert pre_recon._mark_auth_required_if_needed(
            store, "t-anon", "no_auth_context:preauth"
        ) is True
        keys = {
            f["key"]
            for f in store.list_facts("t-anon", category="authentication")
        }
        assert "auth_required_detected" in keys
    finally:
        store.close()


def test_mark_auth_required_no_protected_endpoints(task_root):
    """无任何需认证端点：不误报。"""
    from pobi_agent.recon import ReconStore
    from pobi_v2.engine import pre_recon

    store = ReconStore.for_task("t-clean", str(task_root))
    try:
        store.ensure_session("t-clean", target=TARGET)
        assert pre_recon._mark_auth_required_if_needed(
            store, "t-clean", "external_only"
        ) is False
    finally:
        store.close()
