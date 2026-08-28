"""ReconStore 单元测试：幂等 upsert / WAL / 按 task 隔离 / lookup 命中。"""

from __future__ import annotations

import pytest

from pobi_agent.recon import ReconStore, ReconStoreError
from pobi_agent.recon.sqlite_models import ReconFact


# ---------------------------------------------------------------------------
# WAL 加固
# ---------------------------------------------------------------------------


def test_wal_enabled(tmp_path):
    db = tmp_path / "wal.db"
    store = ReconStore(db)
    # WAL 模式下会生成 -wal / -shm 文件（写入后）。
    store.ensure_session("t")
    store.upsert_fact("t", "endpoint", "/a", "x")
    # 经 engine 连接验证 pragma（foreign_keys 为连接级，由 engine 事件设置）。
    with store._engine.connect() as conn:
        mode = conn.exec_driver_sql("PRAGMA journal_mode").scalar()
        fk = conn.exec_driver_sql("PRAGMA foreign_keys").scalar()
        version = conn.exec_driver_sql("PRAGMA user_version").scalar()
    store.close()
    assert mode.lower() == "wal"
    assert fk == 1
    assert version == 1


# ---------------------------------------------------------------------------
# 幂等 upsert
# ---------------------------------------------------------------------------


def test_upsert_fact_idempotent_higher_confidence_wins(tmp_path):
    store = ReconStore(tmp_path / "idp.db")
    store.ensure_session("t")
    store.upsert_fact("t", "endpoint", "/a", "low", confidence=0.3)
    store.upsert_fact("t", "endpoint", "/a", "high", confidence=0.95)
    with store._session_factory() as s:
        rows = s.execute(
            __import__("sqlalchemy").select(ReconFact).where(ReconFact.key == "/a")
        ).scalars().all()
        assert len(rows) == 1
        assert rows[0].value == "high"
        assert rows[0].confidence == 0.95
    store.close()


def test_upsert_fact_lower_confidence_does_not_overwrite(tmp_path):
    store = ReconStore(tmp_path / "idp2.db")
    store.ensure_session("t")
    store.upsert_fact("t", "endpoint", "/a", "high", confidence=0.95)
    store.upsert_fact("t", "endpoint", "/a", "low", confidence=0.2)
    with store._session_factory() as s:
        row = s.execute(
            __import__("sqlalchemy").select(ReconFact).where(ReconFact.key == "/a")
        ).scalar_one()
        assert row.value == "high"
        assert row.confidence == 0.95
    store.close()


def test_upsert_endpoint_merges_tech_stack(tmp_path):
    store = ReconStore(tmp_path / "ep.db")
    store.ensure_session("t")
    store.upsert_endpoint("t", "/login", tech_stack=["nginx"])
    store.upsert_endpoint("t", "/login", tech_stack=["php", "nginx"])
    with store._session_factory() as s:
        from pobi_agent.recon.sqlite_models import ReconEndpoint

        ep = s.execute(
            __import__("sqlalchemy").select(ReconEndpoint).where(
                ReconEndpoint.path_normalized == "/login"
            )
        ).scalar_one()
        assert set(ep.tech_stack) == {"nginx", "php"}
    store.close()


# ---------------------------------------------------------------------------
# 按 task 隔离
# ---------------------------------------------------------------------------


def test_task_isolation(tmp_path):
    store = ReconStore(tmp_path / "iso.db")
    store.ensure_session("taskA")
    store.ensure_session("taskB")
    store.upsert_fact("taskA", "endpoint", "/onlyA", "a")
    store.upsert_fact("taskB", "endpoint", "/onlyB", "b")
    a = store.lookup("taskA", category="endpoint")
    b = store.lookup("taskB", category="endpoint")
    assert {r["key"] for r in a} == {"/onlyA"}
    assert {r["key"] for r in b} == {"/onlyB"}
    store.close()


# ---------------------------------------------------------------------------
# lookup 命中
# ---------------------------------------------------------------------------


def test_lookup_by_path_prefix(tmp_path):
    store = ReconStore(tmp_path / "lp.db")
    store.ensure_session("t")
    store.upsert_endpoint("t", "/api/v1/users", host="h")
    store.upsert_endpoint("t", "/static/app.js", host="h")
    hits = store.lookup("t", path_prefix="/api/")
    assert len(hits) == 1
    assert hits[0]["path"] == "/api/v1/users"
    store.close()


def test_lookup_by_tech(tmp_path):
    store = ReconStore(tmp_path / "lt.db")
    store.ensure_session("t")
    store.upsert_endpoint("t", "/a", tech_stack=["wordpress"])
    store.upsert_endpoint("t", "/b", tech_stack=["nginx"])
    hits = store.lookup("t", tech="wordpress")
    assert {h["path"] for h in hits} == {"/a"}
    store.close()


# ---------------------------------------------------------------------------
# for_task 路径安全
# ---------------------------------------------------------------------------


def test_for_task_creates_task_level_db(tmp_path):
    store = ReconStore.for_task("taskX", task_root=str(tmp_path))
    # 任务级单一库：tasks/<id>/<id>.db（无 recon/ 子目录）。
    assert (tmp_path / "taskX.db").exists()
    store.close()


def test_for_task_rejects_empty_id(tmp_path):
    with pytest.raises(ReconStoreError):
        ReconStore.for_task("", task_root=str(tmp_path))


def test_for_task_sanitizes_id(tmp_path):
    store = ReconStore.for_task("../evil", task_root=str(tmp_path))
    # 越界字符被清理，库落在 task_root 内（任务级单一库），而非 task_root 之外。
    assert store.db_path.parent == tmp_path
    store.close()
