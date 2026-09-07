"""M3 持久化辅助：把运行轨迹、发现、审计、产物写入数据库。

集中封装写入逻辑，供 executor（Worker）与 routers（API）复用。
所有函数接受外部传入的 AsyncSession，由调用方控制事务边界。
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from uuid import UUID, uuid4

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from pobi_agent.logging import logger
from pobi_v2.core.otel import current_trace_ids
from pobi_v2.db.models import (
    Artifact,
    ArtifactKind,
    AuditEvent,
    Finding,
    Severity,
    Task,
    TaskEvent,
)

# 审计哈希链串行写入的 PG advisory lock 常量（全局唯一，避免并发写串链）
AUDIT_CHAIN_LOCK_KEY = 728_311_045


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _canonical_json(payload: dict) -> str:
    """规范化 JSON：键排序、无空格、非 ASCII 保留，保证同内容同字节。"""
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
    )


def audit_row_hash(payload: dict, prev_hash: str | None) -> str:
    """审计行哈希：``sha256(规范化行内容 + 上一行 hash)``。

    ``payload`` 不含 prev_hash / hash 自身，校验时用链上前值参与计算，
    因此删除或篡改任意一行都会导致后续行校验失败。
    """
    body = _canonical_json(payload) + (prev_hash or "")
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _audit_payload(evt: AuditEvent) -> dict:
    """构造参与哈希计算的审计行内容（与 prev_hash / hash 字段本身无关）。"""
    return {
        "id": str(evt.id),
        "task_id": str(evt.task_id) if evt.task_id else None,
        "target_id": str(evt.target_id) if evt.target_id else None,
        "tenant_id": str(evt.tenant_id) if evt.tenant_id else None,
        "actor_id": str(evt.actor_id) if evt.actor_id else None,
        "actor": evt.actor,
        "action": evt.action,
        "outcome": evt.outcome,
        "detail": evt.detail,
        "meta": evt.meta,
        "created_at": evt.created_at.isoformat() if evt.created_at else None,
    }


async def record_task_event(
    session: AsyncSession,
    task_id: UUID,
    event_type: str,
    payload: dict,
) -> TaskEvent:
    """追加一条有序任务事件（与 SSE 同源的持久化副本）。"""
    seq = await _next_seq(session, task_id)
    trace_id, span_id = current_trace_ids()
    evt = TaskEvent(
        id=uuid4(),
        task_id=task_id,
        seq=seq,
        event_type=event_type,
        payload=payload,
        trace_id=trace_id,
        span_id=span_id,
        created_at=_utcnow(),
    )
    session.add(evt)
    await session.flush()
    return evt


async def _next_seq(session: AsyncSession, task_id: UUID) -> int:
    stmt = select(func.max(TaskEvent.seq)).where(TaskEvent.task_id == task_id)
    result = await session.execute(stmt)
    current = result.scalar()
    return (current + 1) if current is not None else 1


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
    target_id: UUID | None = None,
) -> Artifact:
    """记录一件任务产物（截图 / PoC / 报告 / 日志）。"""
    artifact = Artifact(
        id=uuid4(),
        task_id=task_id,
        finding_id=finding_id,
        target_id=target_id,
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
    *,
    actor: str,
    actor_id: UUID | None = None,
    outcome: str = "success",
    detail: str | None = None,
    task_id: UUID | None = None,
    target_id: UUID | None = None,
    meta: dict | None = None,
    tenant_id: UUID | None = None,
) -> AuditEvent:
    """写入一条结构化审计事件（唯一写入口）。

    ``actor`` 为必填：API 侧传当前用户邮箱，Worker 侧传 ``task.operator``，
    不再有默认值兜底。trace 关联与哈希链在此统一计算，调用方无需感知。
    """
    await _lock_audit_chain(session)
    prev_hash = await _last_audit_hash(session)
    trace_id, span_id = current_trace_ids()
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
        trace_id=trace_id,
        span_id=span_id,
        prev_hash=prev_hash,
        hash=None,
    )
    evt.hash = audit_row_hash(_audit_payload(evt), prev_hash)
    session.add(evt)
    await session.flush()
    return evt


async def record_audit_safe(
    session: AsyncSession,
    action: str,
    *,
    actor: str,
    **kwargs,
) -> AuditEvent | None:
    """写入审计并立即提交；失败仅记日志，不阻断业务主流程。

    治理留痕不得反噬可用性：登录、审批、Worker 执行链路都不得因审计写失败而中断。
    """
    try:
        evt = await record_audit(session, action, actor=actor, **kwargs)
        await session.commit()
        return evt
    except Exception:
        logger.warning("审计写入失败（不阻断主流程）：action=%s", action, exc_info=True)
        try:
            await session.rollback()
        except Exception:
            pass
        return None


async def _lock_audit_chain(session: AsyncSession) -> None:
    """串行化审计写入，避免并发导致哈希链分叉（仅 PG 生效）。"""
    dialect = getattr(session.get_bind(), "dialect", None)
    if getattr(dialect, "name", None) != "postgresql":
        return
    await session.execute(
        text("SELECT pg_advisory_xact_lock(:key)"), {"key": AUDIT_CHAIN_LOCK_KEY}
    )


async def _last_audit_hash(session: AsyncSession) -> str | None:
    stmt = select(AuditEvent.hash).order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc()).limit(1)
    result = await session.execute(stmt)
    return result.scalar()


async def verify_audit_chain(session: AsyncSession, limit: int | None = None) -> dict:
    """重算审计哈希链，检测删除与篡改。

    返回 ``{"checked": n, "ok": bool, "broken_id": UUID | None, "unchained": k}``。
    ``unchained`` 为未纳入链的行数（历史遗留，``hash`` 为空），不视为失败。
    """
    stmt = select(AuditEvent).order_by(AuditEvent.created_at, AuditEvent.id)
    if limit is not None:
        stmt = stmt.limit(limit)
    rows = (await session.execute(stmt)).scalars().all()
    prev: str | None = None
    unchained = 0
    for row in rows:
        if row.hash is None:
            unchained += 1
            prev = row.hash
            continue
        if audit_row_hash(_audit_payload(row), prev) != row.hash:
            return {
                "checked": len(rows),
                "ok": False,
                "broken_id": row.id,
                "unchained": unchained,
            }
        prev = row.hash
    return {"checked": len(rows), "ok": True, "broken_id": None, "unchained": unchained}


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
