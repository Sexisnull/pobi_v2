"""M3 持久化与取消逻辑测试（不依赖 Postgres，验证纯逻辑层）。"""
from __future__ import annotations

import asyncio
import uuid

import pytest

from pobi_v2.engine.cancel_state import clear_cancel, is_cancelled, request_cancel
from pobi_v2.engine.guardrails import assert_in_scope, check_scope, policy_from_target
from pobi_v2.db.models import Severity, ArtifactKind
from pobi_v2.schemas.persistence import (
    FindingRead,
    AuditEventRead,
    TaskEventRead,
    ArtifactRead,
)


def _make_target(url: str, in_scope=None, out_of_scope=None):
    class _T:
        pass

    t = _T()
    t.id = uuid.uuid4()
    t.name = "t"
    t.url = url
    t.in_scope = in_scope or [url]
    t.out_of_scope = out_of_scope or []
    t.allowed_tools = ["curl", "nuclei"]
    t.notes = None
    t.enabled = True
    return t


def test_cancel_state_memory_backend():
    async def _run():
        tid = uuid.uuid4()
        assert await is_cancelled(tid) is False
        await request_cancel(tid)
        assert await is_cancelled(tid) is True
        await clear_cancel(tid)
        assert await is_cancelled(tid) is False

    asyncio.run(_run())


def test_guardrail_in_scope():
    t = _make_target("https://example.com")
    policy = policy_from_target(t)
    allowed, reason = policy.is_allowed("https://example.com/login")
    assert allowed is True


def test_guardrail_out_of_scope():
    t = _make_target("https://example.com")
    policy = policy_from_target(t)
    allowed, reason = policy.is_allowed("https://evil.com")
    assert allowed is False
    assert reason


def test_check_scope_raises_on_violation():
    t = _make_target("https://example.com")
    with pytest.raises(Exception):
        assert_in_scope(t, "https://other.com")


def test_severity_and_artifact_enums():
    assert Severity("high") == Severity.high
    assert ArtifactKind("poc") == ArtifactKind.poc


def test_persistence_schemas_from_attributes():
    # 校验 schema 可接纳 ORM 风格字典
    evt = TaskEventRead.model_validate(
        {
            "id": str(uuid.uuid4()),
            "task_id": str(uuid.uuid4()),
            "seq": 1,
            "event_type": "agent_thought",
            "payload": {"thought": "x"},
            "created_at": "2026-08-10T00:00:00+00:00",
        }
    )
    assert evt.seq == 1
    assert evt.payload["thought"] == "x"

    finding = FindingRead.model_validate(
        {
            "id": str(uuid.uuid4()),
            "task_id": str(uuid.uuid4()),
            "target_id": str(uuid.uuid4()),
            "title": "SQLi",
            "severity": "high",
            "evidence": {},
            "created_at": "2026-08-10T00:00:00+00:00",
        }
    )
    assert finding.severity is Severity.high
