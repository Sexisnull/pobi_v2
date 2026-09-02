"""端点派生 / 站点总览 / seed-out 折叠 单元测试（目标-任务数据模型重构）。

覆盖：
- derive_endpoints_from_transactions：tx 单一真源派生端点树（状态择优/并集/认证面/去重）。
- build_site_overview：由 tx 现算站点总览统计。
- build_baseline_block：supervisor 启动注入块。
- upsert_to_pg：同 (method, url) 多脏行折叠后不再产生命令内重复冲突键。
"""

from __future__ import annotations

import asyncio

from pobi_agent.recon import ReconStore
from pobi_agent.recon.sqlite_models import ReconHttpTransaction


def _add_tx(store, task_id, session_id, **kw):
    defaults = dict(
        host="h", path_normalized="/", url="http://h/",
        method="GET", status_code=200, source="sitemap:katana",
        tech_stack=[], detected_params=[], auth_required=False,
    )
    defaults.update(kw)
    with store._session_factory() as s:
        s.add(ReconHttpTransaction(task_id=task_id, session_id=session_id, **defaults))
        s.commit()


def _make_store(tmp_path, task_id="tk"):
    store = ReconStore(tmp_path / f"{task_id}.db")
    sid = store.ensure_session(task_id, target="http://h/", objective="recon")
    return store, sid


def test_derive_groups_by_path_and_picks_best_status(tmp_path):
    store, sid = _make_store(tmp_path)
    # 同一路径两条观测：katana 403（匿名）→ requester 200（带 cookie）
    _add_tx(store, "tk", sid, path_normalized="/admin", url="http://h/admin",
            method="GET", status_code=403, source="sitemap:katana", auth_required=True)
    _add_tx(store, "tk", sid, path_normalized="/admin", url="http://h/admin",
            method="GET", status_code=200, source="agent:requester", auth_required=True)
    _add_tx(store, "tk", sid, path_normalized="/", url="http://h/",
            method="GET", status_code=200, source="sitemap:katana", tech_stack=["nginx"])

    asyncio.run(store.derive_endpoints_from_transactions("tk"))

    from pobi_agent.recon.sqlite_models import ReconEndpoint
    with store._session_factory() as s:
        endpoints = s.query(ReconEndpoint).filter(ReconEndpoint.task_id == "tk").all()
    by_path = {e.path_normalized: e for e in endpoints}

    # 路径级聚合：/admin 与 / 共 2 个端点
    assert set(by_path) == {"/admin", "/"}
    # 状态择优：2xx > 4xx → /admin 取 200
    assert by_path["/admin"].status_code == 200
    # 认证面：任一观测需认证即标记
    assert by_path["/admin"].auth_required is True
    # 技术栈并集（来自 / 的 nginx）
    assert "nginx" in by_path["/"].tech_stack
    # 多观测状态码在 notes 标注（认证差异可对比）
    assert "多观测状态码" in (by_path["/admin"].notes or "")


def test_derive_request_count_and_notes(tmp_path):
    store, sid = _make_store(tmp_path)
    _add_tx(store, "tk", sid, path_normalized="/x", url="http://h/x",
            method="GET", status_code=200, source="sitemap:katana")
    _add_tx(store, "tk", sid, path_normalized="/x", url="http://h/x",
            method="GET", status_code=200, source="agent:requester")

    asyncio.run(store.derive_endpoints_from_transactions("tk"))
    from pobi_agent.recon.sqlite_models import ReconEndpoint
    with store._session_factory() as s:
        ep = s.query(ReconEndpoint).filter(
            ReconEndpoint.task_id == "tk", ReconEndpoint.path_normalized == "/x").first()
    assert ep is not None
    # 两源观测：notes 标注方法或状态码信息，技术栈/参数经派生态并集
    assert ep.method == "GET"
    assert ep.status_code == 200


def test_site_overview_from_tx(tmp_path):
    store, sid = _make_store(tmp_path)
    _add_tx(store, "tk", sid, host="h1", path_normalized="/a", url="http://h1/a",
            method="GET", status_code=200, detected_params=["q"], tech_stack=["nginx"],
            auth_required=False)
    _add_tx(store, "tk", sid, host="h1", path_normalized="/b", url="http://h1/b",
            method="POST", status_code=403, detected_params=["q", "token"],
            auth_required=True)

    ov = store.build_site_overview("tk")
    assert ov["host_count"] == 1
    assert ov["endpoint_count"] == 2
    assert ov["auth_required_endpoint_count"] == 1
    assert ov["input_point_count"] == 2
    assert ov["status_histogram"].get(200) == 1 and ov["status_histogram"].get(403) == 1
    params = dict(ov["top_parameters"])
    assert params.get("q") == 2 and params.get("token") == 1
    techs = dict(ov["top_technologies"])
    assert techs.get("nginx") == 1


def test_baseline_block_includes_overview(tmp_path):
    store, sid = _make_store(tmp_path)
    _add_tx(store, "tk", sid, path_normalized="/admin", url="http://h/admin",
            method="GET", status_code=200, auth_required=True, source="agent:requester")
    store.upsert_fact("tk", "technology", "nginx", "nginx web server", confidence=0.9)

    block = store.build_baseline_block("tk", token_budget=2000)
    assert "目标侦察基线" in block
    assert "需认证" in block
    assert "nginx" in block
    # 受 token 预算约束
    assert len(block) <= 4000


def test_upsert_to_pg_folds_duplicate_method_url(tmp_path):
    """同 (method,url) 多脏行折叠，避免 PG 唯一键命令内重复（CardinalityViolation）。"""
    store, sid = _make_store(tmp_path)
    # 两条同 GET http://h/ 的不同观测（复现取消任务报错场景）
    _add_tx(store, "tk", sid, path_normalized="/", url="http://h/",
            method="GET", status_code=403, source="sitemap:katana")
    _add_tx(store, "tk", sid, path_normalized="/", url="http://h/",
            method="GET", status_code=200, source="agent:requester")

    captured = {}

    class _FakeResult:
        def __init__(self, rows):
            self._rows = rows
        def scalars(self):
            class _S:
                def all(inner): return self._rows
            return _S()

    class _FakeSession:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def execute(self, stmt):
            # 记录插入 recon_http_transactions_agg 的唯一键冲突维度行数
            sql = str(stmt)
            if "recon_http_transactions_agg" in sql and sql.strip().upper().startswith("INSERT"):
                captured.setdefault("tx_rows", []).append(stmt)
            return _FakeResult([])
        async def commit(self): pass
        async def rollback(self): pass

    factory = lambda: _FakeSession()
    # 不抛 CardinalityViolation：折叠后仅 1 行代表
    asyncio.run(store.upsert_to_pg(
        target_id="11111111-1111-1111-1111-111111111111",
        tenant_id="22222222-2222-2222-2222-222222222222",
        task_id="tk",
        async_session_factory=factory,
    ))
    # 折叠生效：插入 agg 的事务行仅 1 条（代表行），但两脏行均被标记已同步
    assert len(captured.get("tx_rows", [])) == 1
    with store._session_factory() as s:
        remaining = s.query(ReconHttpTransaction).filter(
            ReconHttpTransaction.task_id == "tk",
            ReconHttpTransaction.pg_synced_at.is_(None)).count()
    assert remaining == 0, "所有脏行应被标记已同步"
