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
    stmt = stmt.order_by(AuditEvent.created_at.desc()).limit(limit).offset(offset)
    return list((await session.execute(stmt)).scalars().all())
