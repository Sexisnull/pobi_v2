"""M3 持久化辅助：把运行轨迹、发现、审计、产物写入数据库。

集中封装写入逻辑，供 executor（Worker）与 routers（API）复用。
所有函数接受外部传入的 AsyncSession，由调用方控制事务边界。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pobi_v2.db.models import (
    Artifact,
    ArtifactKind,
    AuditEvent,
    Finding,
    Severity,
    Task,
    TaskEvent,
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def record_task_event(
    session: AsyncSession,
    task_id: UUID,
    event_type: str,
    payload: dict,
) -> TaskEvent:
    """追加一条有序任务事件（与 SSE 同源的持久化副本）。"""
    seq = await _next_seq(session, task_id)
    evt = TaskEvent(
        id=uuid4(),
        task_id=task_id,
        seq=seq,
        event_type=event_type,
        payload=payload,
        created_at=_utcnow(),
    )
    session.add(evt)
    await session.flush()
    return evt


async def _next_seq(session: AsyncSession, task_id: UUID) -> int:
    stmt = select(TaskEvent.seq).where(TaskEvent.task_id == task_id)
    result = await session.execute(stmt)
    rows = result.scalars().all()
    return (max(rows) + 1) if rows else 1


async def record_finding(
    session: AsyncSession,
    task_id: UUID,
    target_id: UUID,
    title: str,
    severity: Severity | str = Severity.info,
    description: str | None = None,
    evidence: dict | None = None,
    confidence: float | None = None,
    cwe: str | None = None,
) -> Finding:
    """记录一条渗透发现（漏洞 / 风险点）。"""
    finding = Finding(
        id=uuid4(),
        task_id=task_id,
        target_id=target_id,
        title=title,
        severity=Severity(severity) if not isinstance(severity, Severity) else severity,
        description=description,
        evidence=evidence or {},
        confidence=confidence,
        cwe=cwe,
        created_at=_utcnow(),
    )
    session.add(finding)
    await session.flush()
    return finding


async def record_artifact(
    session: AsyncSession,
    task_id: UUID,
    name: str,
    kind: ArtifactKind | str = ArtifactKind.other,
    finding_id: UUID | None = None,
    storage_key: str | None = None,
    content: str | None = None,
    content_type: str | None = None,
    size_bytes: int | None = None,
) -> Artifact:
    """记录一件任务产物（截图 / PoC / 报告 / 日志）。"""
    artifact = Artifact(
        id=uuid4(),
        task_id=task_id,
        finding_id=finding_id,
        kind=ArtifactKind(kind) if not isinstance(kind, ArtifactKind) else kind,
        name=name,
        storage_key=storage_key,
        content=content,
        content_type=content_type,
        size_bytes=size_bytes,
        created_at=_utcnow(),
    )
    session.add(artifact)
    await session.flush()
    return artifact


async def record_audit(
    session: AsyncSession,
    action: str,
    actor: str = "web-operator",
    outcome: str = "success",
    detail: str | None = None,
    task_id: UUID | None = None,
    target_id: UUID | None = None,
    meta: dict | None = None,
    tenant_id: UUID | None = None,
    actor_id: UUID | None = None,
) -> AuditEvent:
    """写入一条结构化审计事件。"""
    evt = AuditEvent(
        id=uuid4(),
        task_id=task_id,
        target_id=target_id,
        tenant_id=tenant_id,
        actor_id=actor_id,
        actor=actor,
        action=action,
        outcome=outcome,
        detail=detail,
        meta=meta or {},
        created_at=_utcnow(),
    )
    session.add(evt)
    await session.flush()
    return evt


async def get_task(session: AsyncSession, task_id: UUID) -> Task | None:
    return await session.get(Task, task_id)


def serialize_result(obj) -> str:
    """把 Agent 结果安全序列化为可落库的文本。"""
    if obj is None:
        return ""
    if isinstance(obj, str):
        return obj
    try:
        return json.dumps(obj, default=str, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(obj)
