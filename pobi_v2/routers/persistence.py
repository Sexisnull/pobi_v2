"""M3 持久化查询路由：findings / artifacts / task_events / audit_events。

提供按 task 聚合的发现与轨迹查询，以及全局审计查询。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from uuid import UUID

from pobi_v2.core.deps import get_current_user, require_scope
from pobi_v2.core.exceptions import NotFoundError
from pobi_v2.db.models import Artifact, AuditEvent, Finding, Task, TaskEvent, User
from pobi_v2.db.session import get_session
from pobi_v2.schemas.persistence import (
    ArtifactRead,
    AuditEventRead,
    FindingRead,
    TaskDetailRead,
    TaskEventRangeOut,
    TaskEventRead,
)

router = APIRouter(prefix="/api/v1", tags=["persistence"])


@router.get("/tasks/{task_id}", response_model=TaskDetailRead)
async def get_task_detail(
    task_id: UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_scope("tasks:read")),
) -> Task:
    task = await session.get(Task, task_id)
    if task is None or task.tenant_id != user.tenant_id:
        raise NotFoundError("任务不存在")
    count_stmt = select(func.count()).select_from(TaskEvent).where(
        TaskEvent.task_id == task.id
    )
    task.event_count = (await session.execute(count_stmt)).scalar_one()  # type: ignore[attr-defined]
    return task


@router.get("/tasks/{task_id}/events", response_model=list[TaskEventRead])
async def list_task_events(
    task_id: UUID,
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_scope("tasks:read")),
) -> list[TaskEvent]:
    task = await session.get(Task, task_id)
    if task is None or task.tenant_id != user.tenant_id:
        raise NotFoundError("任务不存在")
    stmt = (
        select(TaskEvent)
        .where(TaskEvent.task_id == task_id)
        .order_by(TaskEvent.seq)
        .limit(limit)
        .offset(offset)
    )
    return list((await session.execute(stmt)).scalars().all())


@router.get("/tasks/{task_id}/events/range", response_model=TaskEventRangeOut)
async def get_task_event_range(
    task_id: UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_scope("tasks:read")),
) -> TaskEventRangeOut:
    """任务事件 seq 区间元信息（回放播放器定位用）。

    只做 count/min/max 聚合，不取 payload。播放器据此把进度条映射到 seq，
    再用 ``GET /tasks/{id}/events?after_seq=&limit=`` 按播放窗口滑动取明细，
    避免长任务一次性加载全量事件。
    """
    task = await session.get(Task, task_id)
    if task is None or task.tenant_id != user.tenant_id:
        raise NotFoundError("任务不存在")
    total, min_seq, max_seq, first_at, last_at = (
        await session.execute(
            select(
                func.count(TaskEvent.id),
                func.min(TaskEvent.seq),
                func.max(TaskEvent.seq),
                func.min(TaskEvent.created_at),
                func.max(TaskEvent.created_at),
            ).where(TaskEvent.task_id == task_id)
        )
    ).one()
    return TaskEventRangeOut(
        task_id=str(task_id),
        total=int(total or 0),
        min_seq=int(min_seq) if min_seq is not None else None,
        max_seq=int(max_seq) if max_seq is not None else None,
        first_at=first_at.isoformat() if first_at else None,
        last_at=last_at.isoformat() if last_at else None,
    )


@router.get("/tasks/{task_id}/findings", response_model=list[FindingRead])
async def list_findings(
    task_id: UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_scope("tasks:read")),
) -> list[Finding]:
    task = await session.get(Task, task_id)
    if task is None or task.tenant_id != user.tenant_id:
        raise NotFoundError("任务不存在")
    stmt = select(Finding).where(Finding.task_id == task_id).order_by(
        Finding.severity.desc(), Finding.created_at
    )
    return list((await session.execute(stmt)).scalars().all())


@router.get("/tasks/{task_id}/artifacts", response_model=list[ArtifactRead])
async def list_artifacts(
    task_id: UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_scope("tasks:read")),
) -> list[Artifact]:
    task = await session.get(Task, task_id)
    if task is None or task.tenant_id != user.tenant_id:
        raise NotFoundError("任务不存在")
    stmt = select(Artifact).where(Artifact.task_id == task_id).order_by(
        Artifact.created_at
    )
    return list((await session.execute(stmt)).scalars().all())


@router.get("/audit", response_model=list[AuditEventRead])
async def list_audit(
    task_id: UUID | None = Query(default=None),
    target_id: UUID | None = Query(default=None),
    action: str | None = Query(default=None),
    actor: str | None = Query(default=None, description="按操作者过滤"),
    trace_id: str | None = Query(default=None, description="按追踪 ID 过滤"),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_scope("audit:read")),
) -> list[AuditEvent]:
    stmt = select(AuditEvent).where(AuditEvent.tenant_id == user.tenant_id)
    if task_id is not None:
        stmt = stmt.where(AuditEvent.task_id == task_id)
    if target_id is not None:
        stmt = stmt.where(AuditEvent.target_id == target_id)
    if action is not None:
        stmt = stmt.where(AuditEvent.action == action)
    if actor is not None:
        stmt = stmt.where(AuditEvent.actor == actor)
    if trace_id is not None:
        stmt = stmt.where(AuditEvent.trace_id == trace_id)
    stmt = stmt.order_by(AuditEvent.created_at.desc()).limit(limit).offset(offset)
    return list((await session.execute(stmt)).scalars().all())
