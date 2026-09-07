"""审计治理增强测试：哈希链防篡改、actor 契约、trace 关联。

用 sqlite 内存库验证写入链路（不依赖 Postgres）；PG 专属的 advisory lock
与 append-only 触发器不在本测试范围。
"""
from __future__ import annotations

import inspect

import pytest
from sqlalchemy import delete, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from pobi_v2.db.models import AuditEvent, Base
from pobi_v2.db.persistence import audit_row_hash, record_audit, verify_audit_chain
from pobi_v2.schemas.persistence import AuditEventRead


@pytest.fixture
async def session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


async def _write(session, action: str, actor: str = "ops@example.com") -> AuditEvent:
    return await record_audit(session, action=action, actor=actor, detail=action)


def test_audit_row_hash_is_deterministic_and_chain_sensitive():
    payload = {"action": "auth.login", "actor": "a@b.c", "meta": {"b": 1, "a": 2}}
    assert audit_row_hash(payload, None) == audit_row_hash(payload, None)
    assert audit_row_hash(payload, None) != audit_row_hash(payload, "prev")
    # 键顺序不影响结果
    reordered = {"meta": {"a": 2, "b": 1}, "actor": "a@b.c", "action": "auth.login"}
    assert audit_row_hash(payload, None) == audit_row_hash(reordered, None)


def test_record_audit_requires_actor():
    """actor 必须是无默认值的仅关键字参数，杜绝 'web-operator' 兜底。"""
    param = inspect.signature(record_audit).parameters["actor"]
    assert param.kind is inspect.Parameter.KEYWORD_ONLY
    assert param.default is inspect.Parameter.empty


async def test_hash_chain_links_rows(session_factory):
    async with session_factory() as session:
        first = await _write(session, "auth.login")
        second = await _write(session, "target.created")
        assert first.prev_hash is None
        assert second.prev_hash == first.hash
        assert first.hash and second.hash

        result = await verify_audit_chain(session)
        assert result["ok"] is True
        assert result["checked"] == 2
        assert result["unchained"] == 0


async def test_verify_detects_tampered_row(session_factory):
    async with session_factory() as session:
        await _write(session, "auth.login")
        target = await _write(session, "target.created")
        await session.execute(
            update(AuditEvent).where(AuditEvent.id == target.id).values(detail="被篡改")
        )
        await session.commit()

        result = await verify_audit_chain(session)
        assert result["ok"] is False


async def test_verify_detects_deleted_row(session_factory):
    async with session_factory() as session:
        await _write(session, "auth.login")
        middle = await _write(session, "target.created")
        await _write(session, "target.updated")
        # 哈希链含前值，删除中间行会导致后续行重算不匹配
        await session.execute(delete(AuditEvent).where(AuditEvent.id == middle.id))
        await session.commit()

        result = await verify_audit_chain(session)
        assert result["ok"] is False


async def test_record_audit_fills_trace_from_current_span(session_factory, monkeypatch):
    import pobi_v2.db.persistence as persistence

    monkeypatch.setattr(
        persistence, "current_trace_ids", lambda: ("a" * 32, "b" * 16)
    )
    async with session_factory() as session:
        evt = await _write(session, "approval.decision", actor="admin@example.com")
        assert evt.trace_id == "a" * 32
        assert evt.span_id == "b" * 16


def test_audit_read_schema_exposes_actor_and_trace():
    read = AuditEventRead.model_validate(
        {
            "id": "00000000-0000-0000-0000-000000000000",
            "task_id": None,
            "target_id": None,
            "actor_id": "11111111-1111-1111-1111-111111111111",
            "actor": "ops@example.com",
            "action": "agent.high_risk_tool",
            "outcome": "denied",
            "detail": None,
            "meta": {"tool_name": "execute_command"},
            "trace_id": "a" * 32,
            "span_id": "b" * 16,
            "created_at": "2026-09-07T00:00:00+00:00",
        }
    )
    assert read.actor_id is not None
    assert read.trace_id == "a" * 32
