"""RECON 任务状态/数据查询 API 测试（不依赖 Postgres）。

通过 dependency_overrides 注入伪 session/user 绕过 PG 与鉴权，直接对
tasks router 的 /api/v1/tasks/{task_id}/recon/* 只读端点做结构化断言，并
用 ReconStore 在临时 TASKS_ROOT 下构造样本本地库验证过滤/404/缺失场景。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import pobi_agent.constants as agent_constants
from pobi_agent.recon.store import ReconStore


class _FakeUser:
    """最小 User 替身：仅需 tenant_id 与有效 scopes（全权限）。"""

    def __init__(self, tenant_id: str) -> None:
        self.id = "user-test"
        self.tenant_id = tenant_id
        self.email = "tester@example.com"
        setattr(self, "_effective_scopes", ["*"])


class _FakeSession:
    """最小 AsyncSession 替身：按 task_id 返回构造的 Task（tenant 可配置）。

    id 直接回显传入的 ident（URL 路径 task_id），保证 _recon_store 定位的
    本地库路径与 seed 路径一致；tenant_id 不匹配调用方时由 _load_or_404 拒绝。
    """

    def __init__(self, tenant_id: str) -> None:
        self._tenant_id = tenant_id

    async def get(self, model, ident):  # noqa: ANN001, ANN201
        if model.__name__ == "Task":
            return type("Task", (), {"id": ident, "tenant_id": self._tenant_id})()
        return None


def _seed_sample_db(task_id: str, task_root: str) -> None:
    """写入样本本地库：2 主机、3 端点、4 事实、3 威胁（含状态/严重度分布）。"""
    store = ReconStore.for_task(task_id, task_root=task_root)
    store.upsert_endpoint(task_id, "/api/login", host="api.example.com", method="POST",
                          status_code=200, auth_required=True, tech_stack=["nginx"], confidence=0.9)
    store.upsert_endpoint(task_id, "/admin", host="api.example.com", method="GET",
                          status_code=403, auth_required=True, tech_stack=["nginx", "django"], confidence=0.8)
    store.upsert_endpoint(task_id, "/", host="www.example.com", method="GET",
                          status_code=200, tech_stack=["wordpress"], confidence=0.6)
    store.upsert_fact(task_id, "technology", "ssh", "22", confidence=0.9, source="nmap")
    store.upsert_fact(task_id, "technology", "https", "443", confidence=0.8, source="nmap")
    store.upsert_fact(task_id, "credential", "weak-password", "admin:admin", confidence=0.5)
    store.upsert_fact(task_id, "finding", "open-admin", "/admin 可匿名访问", confidence=0.7)
    store.upsert_threat(task_id, "CVE-2024-0001", "RCE in nginx", category="rce",
                        severity="critical", status="exploited", cvss_score=9.8,
                        affected_endpoint="/api/login", evidence_summary="PoC 命中", confidence=0.9)
    store.upsert_threat(task_id, "CVE-2024-0002", "SQLi in admin", category="sqli",
                        severity="high", status="confirmed", cvss_score=8.1,
                        affected_endpoint="/admin", confidence=0.8)
    store.upsert_threat(task_id, "", "弱口令 admin", category="auth",
                        severity="medium", status="suspected", cvss_score=None,
                        affected_endpoint="/api/login", confidence=0.5)


@pytest.fixture()
def client(monkeypatch, tmp_path):  # type: ignore[no-untyped-def]
    from pobi_v2.main import app
    from pobi_v2.core.deps import get_current_user, get_session

    async def _noop(*_a, **_k):  # type: ignore[no-untyped-def]
        return None

    import pobi_v2.main as main_mod

    monkeypatch.setattr(main_mod, "ensure_shared_kali_ready", _noop)
    monkeypatch.setattr(main_mod, "seed_admin_if_needed", _noop)
    monkeypatch.setattr(main_mod, "persist_event_worker", _noop)

    monkeypatch.setattr(agent_constants, "TASKS_ROOT", tmp_path)
    # recon_access 在导入时以 `from ... import TASKS_ROOT` 绑定了常量副本，
    # monkeypatch 模块属性无法穿透，需同步覆盖其模块级引用（tasks 路由经
    # engine.recon_access 间接访问本地库，不再直接持有 TASKS_ROOT）。
    import pobi_v2.engine.recon_access as recon_access_mod

    monkeypatch.setattr(recon_access_mod, "TASKS_ROOT", tmp_path)

    def _install(tenant_id: str = "tenant-test"):  # type: ignore[no-untyped-def]
        async def _fake_session():  # type: ignore[no-untyped-def]
            return _FakeSession(tenant_id)

        async def _fake_user():  # type: ignore[no-untyped-def]
            return _FakeUser(tenant_id)

        app.dependency_overrides[get_session] = _fake_session
        app.dependency_overrides[get_current_user] = _fake_user

    def _seed(task_id: str) -> None:
        _seed_sample_db(task_id, str(tmp_path / task_id))

    with TestClient(app) as c:
        yield c, _install, _seed

    app.dependency_overrides.clear()


def test_recon_summary_structure(client):  # type: ignore[no-untyped-def]
    c, install, seed = client
    task_id = "11111111-1111-1111-1111-111111111111"
    install()
    seed(task_id)

    resp = c.get(f"/api/v1/tasks/{task_id}/recon/summary")
    assert resp.status_code == 200
    data = resp.json()
    assert data["facts_count"] == 4
    assert data["endpoints_count"] == 3
    assert data["threats_count"] == 3
    assert data["assets"]["hosts"] == 2
    assert data["threats_by_status"]["exploited"] == 1
    assert data["threats_by_status"]["confirmed"] == 1
    assert data["threats_by_status"]["suspected"] == 1
    assert data["threats_by_severity"]["critical"] == 1
    assert data["threats_by_severity"]["high"] == 1


def test_recon_facts_category_filter(client):  # type: ignore[no-untyped-def]
    c, install, seed = client
    task_id = "22222222-2222-2222-2222-222222222222"
    install()
    seed(task_id)

    resp = c.get(f"/api/v1/tasks/{task_id}/recon/facts?category=technology")
    assert resp.status_code == 200
    items = resp.json()
    assert len(items) == 2
    assert items[0]["category"] == "technology"
    assert items[0]["key"] == "ssh"

    resp_all = c.get(f"/api/v1/tasks/{task_id}/recon/facts")
    assert len(resp_all.json()) == 4


def test_recon_threats_status_filter_and_detail(client):  # type: ignore[no-untyped-def]
    c, install, seed = client
    task_id = "33333333-3333-3333-3333-333333333333"
    install()
    seed(task_id)

    resp = c.get(f"/api/v1/tasks/{task_id}/recon/threats?status=exploited")
    assert resp.status_code == 200
    items = resp.json()
    assert len(items) == 1
    assert items[0]["cve_id"] == "CVE-2024-0001"
    assert items[0]["evidence_summary"] == "PoC 命中"  # 解密链路验证

    detail = c.get(f"/api/v1/tasks/{task_id}/recon/threats/CVE-2024-0001")
    assert detail.status_code == 200
    assert detail.json()["cvss_score"] == 9.8

    miss = c.get(f"/api/v1/tasks/{task_id}/recon/threats/CVE-9999-9999")
    assert miss.status_code == 404


def test_recon_endpoints_and_assets(client):  # type: ignore[no-untyped-def]
    c, install, seed = client
    task_id = "44444444-4444-4444-4444-444444444444"
    install()
    seed(task_id)

    eps = c.get(f"/api/v1/tasks/{task_id}/recon/endpoints")
    assert eps.status_code == 200
    assert len(eps.json()) == 3

    assets = c.get(f"/api/v1/tasks/{task_id}/recon/assets")
    assert assets.status_code == 200
    a = assets.json()
    assert len(a["hosts"]) == 2
    assert len(a["services"]) == 3  # 每个 endpoint 视为一个暴露服务入口
    assert all(s["host"] for s in a["services"])


def test_recon_coverage_block(client):  # type: ignore[no-untyped-def]
    c, install, seed = client
    task_id = "55555555-5555-5555-5555-555555555555"
    install()
    seed(task_id)

    cov = c.get(f"/api/v1/tasks/{task_id}/recon/coverage")
    assert cov.status_code == 200
    d = cov.json()
    assert "/api/login" in d["covered_endpoints"]
    assert "CVE-2024-0001" in d["covered_threats"]
    assert d["already_covered_count"] >= 3


def test_recon_missing_db_returns_empty(client):  # type: ignore[no-untyped-def]
    """本地库不存在时返回 200 + 空结构（前端友好，不 5xx）。"""
    c, install, seed = client
    task_id = "66666666-6666-6666-6666-666666666666"  # 未 seed
    install()

    summary = c.get(f"/api/v1/tasks/{task_id}/recon/summary")
    assert summary.status_code == 200
    assert summary.json()["facts_count"] == 0
    assert summary.json()["threats_count"] == 0

    threats = c.get(f"/api/v1/tasks/{task_id}/recon/threats")
    assert threats.status_code == 200
    assert threats.json() == []

    detail = c.get(f"/api/v1/tasks/{task_id}/recon/threats/CVE-0000-0001")
    assert detail.status_code == 404  # 单威胁未命中仍为 404


def test_recon_tenant_isolation(client):  # type: ignore[no-untyped-def]
    """跨租户请求：fake session 返回不同 tenant 的 Task，_load_or_404 应拒绝 404。"""
    c, install, seed = client
    task_id = "77777777-7777-7777-7777-777777777777"
    install(tenant_id="tenant-other")  # user 属 tenant-other，但 task 属 tenant-test
    from pobi_v2.main import app
    from pobi_v2.core.deps import get_current_user, get_session

    async def _fake_session():  # type: ignore[no-untyped-def]
        return _FakeSession("tenant-test")  # task 属不同租户

    async def _fake_user():  # type: ignore[no-untyped-def]
        return _FakeUser("tenant-other")

    app.dependency_overrides[get_session] = _fake_session
    app.dependency_overrides[get_current_user] = _fake_user

    resp = c.get(f"/api/v1/tasks/{task_id}/recon/summary")
    assert resp.status_code == 404
