"""认证前置服务（PreAuth）单元测试：不调用真实 LLM / 浏览器 / Docker。

覆盖：
- 认证结果归类（success / mfa / failed / aborted）
- run_auto_auth（LLM 驱动 PreAuthAgent）异常安全与 recon_facts 落库
- 创建前凭据预检（verify_credentials）
- 自动填表登录（_auto_submit_login）成功/失败判定
- 登录面观察（observe_login_surface）URL 决策与结果合并
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from pobi_agent.agents.factory import AgentOutput
from pobi_agent.recon.store import ReconStore
from pobi_agent.tools.browser.authenticate import _auto_submit_login
from pobi_v2.engine import preauth


def _status_of(d: dict) -> str:
    return d.get("status", "failed")


def _patch_preauth_agent(monkeypatch, behavior: str = "success"):
    """用 FakePreAuthAgent 替换 PreAuthAgent 并 stub get_model_spec。

    behavior:
    - success: run 模拟 authenticate 落盘 preauth.playwright.json → status=success
    - failed : run 返回无 MFA 标记的失败摘要 → status=failed
    - mfa    : run 返回 requires_interactive_auth 标记 → status=mfa
    - boom   : run 抛异常 → status=failed
    """
    import pobi_agent.agents.generic_agents.preauth_agent as pa_mod
    import pobi_v2.llm as llm_mod

    instances: list = []

    class FakePreAuthAgent:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.run_kwargs = None
            instances.append(self)

        async def run(self, **kwargs):
            self.run_kwargs = kwargs
            task_root = Path(kwargs["deps"].task_root)
            if behavior == "boom":
                raise RuntimeError("browser crash")
            if behavior == "mfa":
                out = AgentOutput(
                    detailed_summary="requires_interactive_auth=true 需要短信验证码",
                    proofs="", confidence_score=0.1, thoughts="",
                )
            elif behavior == "failed":
                out = AgentOutput(
                    detailed_summary="登录失败：表单字段不匹配", proofs="",
                    confidence_score=0.1, thoughts="",
                )
            else:
                p = task_root / "agent" / "auth_context" / "preauth.playwright.json"
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text("{}", encoding="utf-8")
                out = AgentOutput(
                    detailed_summary="登录成功", proofs="", confidence_score=0.9, thoughts="",
                )
            return SimpleNamespace(output=out, error=None)

    monkeypatch.setattr(pa_mod, "PreAuthAgent", FakePreAuthAgent)
    monkeypatch.setattr(llm_mod, "get_model_spec", lambda: SimpleNamespace())
    return instances


def test_status_classify_success():
    r = preauth._auth_status_from_result({"success": True})
    assert r[0] == "success"


def test_status_classify_aborted():
    r = preauth._auth_status_from_result({"success": False, "status": "aborted", "error": "熔断"})
    assert r[0] == "failed"
    assert "熔断" in r[1]


def test_status_classify_mfa_hint():
    r = preauth._auth_status_from_result(
        {"success": False, "error": "需要 MFA 二次验证 code", "detail": "x"}
    )
    assert r[0] == "mfa"


def test_status_classify_failed():
    r = preauth._auth_status_from_result({"success": False, "error": "invalid credentials"})
    assert r[0] == "failed"


def test_status_classify_mfa_cn_hint():
    r = preauth._auth_status_from_result({"success": False, "error": "页面出现验证码拦截"})
    assert r[0] == "mfa"


async def test_run_auto_auth_writes_recon_facts(tmp_path, monkeypatch):
    """LLM 认证成功后 recon_facts 落库（category=authentication，source=preauth_auto）。"""
    instances = _patch_preauth_agent(monkeypatch, behavior="success")

    result = await preauth.run_auto_auth(
        task_id="t-1",
        task_root=Path(tmp_path),
        target="https://example.com",
        login_url="https://example.com/login",
        username="user",
        password="pass",
        profile="preauth",
    )
    assert _status_of(result) == "success"
    assert result["profile"] == "preauth"
    assert result["storage_path"] == str(Path(tmp_path) / "agent" / "auth_context" / "preauth.playwright.json")
    # 走 LLM 驱动：PreAuthAgent 被构造（requires_approval=False）并运行，凭据不进 prompt
    assert instances and instances[0].kwargs["requires_approval"] is False
    assert instances[0].run_kwargs is not None
    prompt = instances[0].run_kwargs["prompt"]
    assert "preauth" in prompt
    assert "pass" not in prompt and "user" not in prompt.split("目标：")[0]

    store = ReconStore.for_task("t-1", str(tmp_path))
    store.ensure_session("t-1", target="https://example.com")
    rows = store.list_facts("t-1", category="authentication")
    keys = {r["key"] for r in rows}
    assert "auth_profile" in keys
    assert "auth_status" in keys
    profile_row = next(r for r in rows if r["key"] == "auth_profile")
    assert profile_row["value"] == "preauth"
    assert profile_row["source"] == "preauth_auto"


async def test_run_auto_auth_exception_degrades(tmp_path, monkeypatch):
    """PreAuthAgent 抛异常时归类 failed，不向上抛，不阻断调用链。"""
    _patch_preauth_agent(monkeypatch, behavior="boom")

    result = await preauth.run_auto_auth(
        task_id="t-2",
        task_root=Path(tmp_path),
        target="https://example.com",
        login_url=None,
        username="u",
        password="p",
    )
    assert _status_of(result) == "failed"
    assert "browser crash" in (result.get("error") or "")


async def test_run_auto_auth_mfa_degrades(tmp_path, monkeypatch):
    """LLM 报告 MFA 拦截归类为 mfa（降级提示人工分支）。"""
    _patch_preauth_agent(monkeypatch, behavior="mfa")

    result = await preauth.run_auto_auth(
        task_id="t-3",
        task_root=Path(tmp_path),
        target="https://example.com",
        login_url=None,
        username="u",
        password="p",
    )
    assert _status_of(result) == "mfa"


# [DISABLED 2026-09-01] 手动登录分支搁置（MFA 人工流程暂缓，见 .ai/roadmap.md MFA 演进计划）
# def test_manual_session_ttl_expired():
#     s = preauth.ManualAuthSession(
#         task_id="t-4",
#         task_root=Path("/tmp/preauth-test"),
#         target="https://example.com",
#         login_url="https://example.com/login",
#     )
#     # 手动改启动时间戳模拟超时
#     s._started_mono -= preauth.MANUAL_SESSION_TTL_S + 1
#     assert s.expired()
#
#
# def test_manual_session_ttl_fresh():
#     s = preauth.ManualAuthSession(
#         task_id="t-5",
#         task_root=Path("/tmp/preauth-test"),
#         target="https://example.com",
#         login_url="https://example.com/login",
#     )
#     assert not s.expired()


# ---------------------------------------------------------------------------
# 创建前凭据预检（verify_credentials）
# ---------------------------------------------------------------------------


async def test_verify_credentials_success(monkeypatch):
    """登录成功 → valid=True，status=success。"""
    async def fake_authenticate_service(**kwargs):
        return {"success": True, "status": "ok"}

    monkeypatch.setattr(preauth, "authenticate_service", fake_authenticate_service)
    result = await preauth.verify_credentials(
        target="https://example.com", login_url=None, username="u", password="p"
    )
    assert result["status"] == "success"
    assert result["valid"] is True
    assert result["ok"] is True


async def test_verify_credentials_bad_credential(monkeypatch):
    """账号密码错误 → valid=False，status=failed，message 提示凭据错误。"""
    async def fake_authenticate_service(**kwargs):
        return {"success": False, "error": "invalid username or password"}

    monkeypatch.setattr(preauth, "authenticate_service", fake_authenticate_service)
    result = await preauth.verify_credentials(
        target="https://example.com", login_url=None, username="u", password="p"
    )
    assert result["status"] == "failed"
    assert result["valid"] is False
    assert "错误" in result["message"]


async def test_verify_credentials_mfa(monkeypatch):
    """检测到二次验证 → valid=False，status=mfa，提示人工登录。"""
    async def fake_authenticate_service(**kwargs):
        return {"success": False, "error": "需要 MFA 二次验证"}

    monkeypatch.setattr(preauth, "authenticate_service", fake_authenticate_service)
    result = await preauth.verify_credentials(
        target="https://example.com", login_url=None, username="u", password="p"
    )
    assert result["status"] == "mfa"
    assert result["valid"] is False
    assert "手动登录" in result["message"]


async def test_verify_credentials_aborted(monkeypatch):
    """框架无法处理该登录形态（熔断）→ status=aborted，提示改手动。"""
    async def fake_authenticate_service(**kwargs):
        return {"success": False, "aborted": True, "error": "连续失败熔断"}

    monkeypatch.setattr(preauth, "authenticate_service", fake_authenticate_service)
    result = await preauth.verify_credentials(
        target="https://example.com", login_url=None, username="u", password="p"
    )
    assert result["status"] == "aborted"
    assert result["valid"] is False


async def test_verify_credentials_exception(monkeypatch):
    """验证过程异常 → status=error，valid=False，不向上抛。"""
    async def boom(**kwargs):
        raise RuntimeError("browser crash")

    monkeypatch.setattr(preauth, "authenticate_service", boom)
    result = await preauth.verify_credentials(
        target="https://example.com", login_url=None, username="u", password="p"
    )
    assert result["status"] == "error"
    assert result["valid"] is False
    assert "browser crash" in result["message"]


async def test_verify_credentials_no_persistence(tmp_path, monkeypatch):
    """预检使用临时目录与唯一 profile：不触碰任务 auth_context、不残留临时目录。"""

    real_mkdtemp = preauth.tempfile.mkdtemp
    tmp_dirs: list[str] = []

    def fake_mkdtemp(*args, **kwargs):
        d = real_mkdtemp(*args, **kwargs)
        tmp_dirs.append(d)
        return d

    monkeypatch.setattr(preauth.tempfile, "mkdtemp", fake_mkdtemp)
    monkeypatch.setattr(preauth.shutil, "rmtree", lambda p, **kw: None)  # 禁用清理以观察残留

    called: dict = {}

    async def fake_authenticate_service(**kwargs):
        called.update(kwargs)
        return {"success": True, "status": "ok"}

    monkeypatch.setattr(preauth, "authenticate_service", fake_authenticate_service)
    result = await preauth.verify_credentials(
        target="https://example.com", login_url=None, username="u", password="p"
    )
    assert result["status"] == "success"
    # 预检 profile 必须是独立临时 profile，绝不使用 preauth 专用 profile
    assert called.get("profile", "").startswith("verify_")
    assert called.get("profile") != "preauth"
    # 使用了临时目录作为 task_root
    assert len(tmp_dirs) == 1


# ---------------------------------------------------------------------------
# 自动填表登录（authenticate_service auto_submit 模式）
# ---------------------------------------------------------------------------


async def test_run_auto_auth_uses_preauth_agent(monkeypatch):
    """run_auto_auth 走 LLM 驱动（PreAuthAgent），不再直接调 authenticate_service。

    authenticate_service 被替换为抛异常：若 run_auto_auth 仍直接调用它，
    认证必然失败；走 PreAuthAgent（成功落盘）则 status=success。
    """
    _patch_preauth_agent(monkeypatch, behavior="success")

    async def boom(**kwargs):
        raise AssertionError("run_auto_auth 不应直接调用 authenticate_service")

    monkeypatch.setattr(preauth, "authenticate_service", boom)
    result = await preauth.run_auto_auth(
        task_id="t-6",
        task_root=Path("/tmp/preauth-test"),
        target="https://example.com",
        login_url="https://example.com/login",
        username="u",
        password="p",
    )
    assert _status_of(result) == "success"


async def test_verify_credentials_enables_auto_submit(monkeypatch):
    """verify_credentials 必须启用 auto_submit。"""
    called: dict = {}

    async def fake_authenticate_service(**kwargs):
        called.update(kwargs)
        return {"success": True, "status": "ok"}

    monkeypatch.setattr(preauth, "authenticate_service", fake_authenticate_service)
    await preauth.verify_credentials(target="https://example.com", login_url=None, username="u", password="p")
    assert called.get("auto_submit") is True


class _FakeBrowser:
    """按脚本特征分发 execute_script 结果的 BrowserSession 替身（模拟 Pydoll 返回 JSON 字符串）。"""

    def __init__(self, fill, still_login=True, err_hint=None, url="http://t/login.php", cookies=()):
        self.fill = fill
        self.still_login = still_login
        self.err_hint = err_hint
        self.url = url
        self.cookies = list(cookies)

    async def execute_script(self, script, page=None):
        if "querySelectorAll('input')" in script:
            return json.dumps(self.fill)  # 模拟真实 Pydoll：对象以 JSON 字符串返回
        if "input[type=password]" in script:
            return self.still_login
        if "querySelectorAll('.error" in script:
            return self.err_hint
        return None

    async def get_url(self, page=None):
        return self.url

    async def get_cookies(self, page=None):
        return [{"name": c} for c in self.cookies]


async def test_auto_submit_no_fields():
    """无法定位表单字段 → 失败并给出原因。"""
    b = _FakeBrowser(fill={"ok": False, "reason": "no-fields"})
    r = await _auto_submit_login(
        browser=b, page=None, username="u", password="p",
        before_url="http://t/login.php", before_cookies=set(),
        navigation_timeout_ms=1000, post_login_wait_ms=500,
    )
    assert r["success"] is False
    assert "no-fields" in r["error"]


async def test_auto_submit_script_exception():
    """填表脚本执行异常 → 失败，不抛给调用链。"""
    class Boom:
        async def execute_script(self, script, page=None):
            raise RuntimeError("evaluate failed")
        async def get_url(self, page=None): return "x"
        async def get_cookies(self, page=None): return []

    r = await _auto_submit_login(
        browser=Boom(), page=None, username="u", password="p",
        before_url="http://t/login.php", before_cookies=set(),
        navigation_timeout_ms=1000, post_login_wait_ms=500,
    )
    assert r["success"] is False
    assert "evaluate failed" in r["error"]


async def test_auto_submit_success_url_changed():
    """提交后 URL 离开登录页且无 password 输入框 → 成功。"""
    b = _FakeBrowser(fill={"ok": True, "method": "click"}, still_login=False, url="http://t/index.php")
    r = await _auto_submit_login(
        browser=b, page=None, username="u", password="p",
        before_url="http://t/login.php", before_cookies={"PHPSESSID"},
        navigation_timeout_ms=2000, post_login_wait_ms=500,
    )
    assert r["success"] is True
    assert "url-changed" in r["matched"]


async def test_auto_submit_success_new_cookie():
    """提交后出现新会话 cookie → 成功。"""
    b = _FakeBrowser(
        fill={"ok": True, "method": "submit"}, still_login=True,
        url="http://t/login.php", cookies=("PHPSESSID", "sessionid"),
    )
    r = await _auto_submit_login(
        browser=b, page=None, username="u", password="p",
        before_url="http://t/login.php", before_cookies={"PHPSESSID"},
        navigation_timeout_ms=2000, post_login_wait_ms=500,
    )
    assert r["success"] is True
    assert "new-cookie" in r["matched"]


async def test_auto_submit_error_hint():
    """停留登录页且出现错误提示 → 失败并返回提示文案。"""
    b = _FakeBrowser(fill={"ok": True}, still_login=True, err_hint="Invalid username or password", url="http://t/login.php")
    r = await _auto_submit_login(
        browser=b, page=None, username="u", password="p",
        before_url="http://t/login.php", before_cookies=set(),
        navigation_timeout_ms=2000, post_login_wait_ms=500,
    )
    assert r["success"] is False
    assert "Invalid" in r["error"]


async def test_auto_submit_timeout():
    """提交后无任何成功信号也无错误提示 → 超时失败。"""
    b = _FakeBrowser(fill={"ok": True}, still_login=True, err_hint=None, url="http://t/login.php")
    r = await _auto_submit_login(
        browser=b, page=None, username="u", password="p",
        before_url="http://t/login.php", before_cookies=set(),
        navigation_timeout_ms=500, post_login_wait_ms=500,
    )
    assert r["success"] is False
    assert "超时" in r["error"]


# ---------------------------------------------------------------------------
# 凭据落任务目录（不落库，仅文件供 authenticator 重认证消费）
# ---------------------------------------------------------------------------


def test_save_task_credentials_writes_task_wallet(tmp_path):
    """save_task_credentials 只写任务目录钱包，凭据随任务走、不落库。"""
    from pobi_agent.auth_resolver import CredentialsStore

    wallet = preauth.save_task_credentials(
        task_root=Path(tmp_path),
        target="https://pwn.example:8081/login",
        username="admin",
        password="s3cret!",
        login_url="https://pwn.example:8081/login.php",
    )
    assert wallet == Path(tmp_path) / "reusable_credentials.json"
    assert wallet.exists()
    data = json.loads(wallet.read_text(encoding="utf-8"))
    creds = data["targets"]["pwn.example:8081"]["credentials"]["preauth"]
    assert creds["username"] == "admin"
    assert creds["password"] == "s3cret!"

    # 任务外（无 task_root 注入）不读任务钱包 —— 凭据隔离在任务目录内
    assert CredentialsStore.resolve("https://pwn.example:8081/login", "preauth").password is None


async def test_credentials_store_resolves_task_wallet(tmp_path):
    """authenticator 重认证时（task_root 已注入）能从任务目录钱包读到凭据。"""
    from pobi_agent.auth_resolver import CredentialsStore
    from pobi_agent.storage_context import clear_task_root, set_task_root

    preauth.save_task_credentials(
        task_root=Path(tmp_path),
        target="https://pwn.example/login",
        username="admin",
        password="s3cret!",
    )
    token = set_task_root(Path(tmp_path))
    try:
        creds = CredentialsStore.resolve("https://pwn.example/login", "preauth")
        assert creds.username == "admin"
        assert creds.password == "s3cret!"
    finally:
        clear_task_root(token)


def test_write_auth_facts_no_username(tmp_path):
    """认证事实不再携带 username（凭据不落 recon sqlite）。"""
    task_id = "t-facts"
    preauth._write_auth_facts(
        task_id=task_id,
        task_root=Path(tmp_path),
        target="https://example.com",
        profile="preauth",
        source="preauth_auto",
        auth_status="success",
        username="admin",  # 传入但不应落库
        error=None,
    )
    store = ReconStore.for_task(task_id, str(tmp_path))
    store.ensure_session(task_id, target="https://example.com")
    rows = store.list_facts(task_id, category="authentication")
    status_row = next(r for r in rows if r["key"] == "auth_status")
    details = status_row.get("details") or {}
    assert "username" not in details
    assert "admin" not in json.dumps(status_row)


# ---------------------------------------------------------------------------
# 登录面观察（observe_login_surface）
# ---------------------------------------------------------------------------


async def test_observe_login_surface_merges_probe_and_observe(monkeypatch):
    """HTTP 探测与浏览器观察结果合并；缺省登录地址回退 deps.login_url。"""
    import importlib
    obs_mod = importlib.import_module("pobi_agent.tools.browser.observe_login_surface")
    from pobi_agent.utils.structures import PreAuthDeps

    ctx = SimpleNamespace(
        deps=PreAuthDeps(target="https://example.com", login_url="https://example.com/login")
    )

    async def fake_probe(url, verify_ssl, timeout_s=10.0):
        return {"http_basic": True, "probe_status": 401, "probe_content_type": "text/html", "probe_final_url": url}

    async def fake_observe(*, url, proxy_url=None, verify_ssl=False, navigation_timeout_ms=None):
        return {"success": True, "inputs": [{"name": "user", "type": "text"}], "has_pw": True, "final_url": url}

    monkeypatch.setattr(obs_mod, "_probe_http_basic", fake_probe)
    monkeypatch.setattr(obs_mod, "_observe_login_surface", fake_observe)

    r = await obs_mod.observe_login_surface(ctx)
    assert r["http_basic"] is True
    assert r["success"] is True
    assert r["final_url"] == "https://example.com/login"


async def test_observe_login_surface_falls_back_to_target(monkeypatch):
    """无 login_url 时回退 deps.target。"""
    import importlib
    obs_mod = importlib.import_module("pobi_agent.tools.browser.observe_login_surface")
    from pobi_agent.utils.structures import PreAuthDeps

    ctx = SimpleNamespace(deps=PreAuthDeps(target="https://example.com"))
    seen: dict = {}

    async def fake_probe(url, verify_ssl, timeout_s=10.0):
        seen["probe_url"] = url
        return {"http_basic": False, "probe_status": 200, "probe_content_type": "", "probe_final_url": url}

    async def fake_observe(**kwargs):
        seen["observe_url"] = kwargs["url"]
        return {"success": True, "final_url": kwargs["url"]}

    monkeypatch.setattr(obs_mod, "_probe_http_basic", fake_probe)
    monkeypatch.setattr(obs_mod, "_observe_login_surface", fake_observe)

    r = await obs_mod.observe_login_surface(ctx)
    assert r["success"] is True
    assert seen["probe_url"] == "https://example.com"
    assert seen["observe_url"] == "https://example.com"


async def test_observe_login_surface_observe_failure(monkeypatch):
    """浏览器观察失败 → success=false 且保留探测信息。"""
    import importlib
    obs_mod = importlib.import_module("pobi_agent.tools.browser.observe_login_surface")
    from pobi_agent.utils.structures import PreAuthDeps

    ctx = SimpleNamespace(deps=PreAuthDeps(target="https://example.com"))

    async def fake_probe(url, verify_ssl, timeout_s=10.0):
        return {"http_basic": False, "probe_status": 200, "probe_content_type": "", "probe_final_url": url}

    async def fake_observe(**kwargs):
        return {"success": False, "error": "导航失败"}

    monkeypatch.setattr(obs_mod, "_probe_http_basic", fake_probe)
    monkeypatch.setattr(obs_mod, "_observe_login_surface", fake_observe)

    r = await obs_mod.observe_login_surface(ctx)
    assert r["success"] is False
    assert "导航失败" in r["error"]
    assert r["http_basic"] is False
