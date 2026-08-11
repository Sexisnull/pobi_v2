"""Task CRUD 路由（含 M2 异步执行触发）。

创建任务时：校验授权范围 -> 状态置 queued -> 入队 ARQ，由 Worker 异步执行并推流。
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pobi_v2.core.deps import get_current_user
from pobi_v2.core.exceptions import NotFoundError
from pobi_v2.db.models import Task, TaskStatus, User
from pobi_v2.db.session import get_session
from pobi_v2.db.persistence import record_audit
from pobi_v2.schemas.task import TaskCreate, TaskRead, TaskUpdate
from pobi_v2.engine.queue import enqueue_task
from pobi_v2.engine.guardrails import check_scope
from pobi_v2.engine.cancel_state import request_cancel

router = APIRouter(prefix="/api/v1/tasks", tags=["tasks"], dependencies=[Depends(get_current_user)])


@router.post("", response_model=TaskRead, status_code=status.HTTP_201_CREATED)
async def create_task(
    data: TaskCreate,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(get_current_user),
) -> Task:
    # 校验 target 存在且属于当前租户
    from pobi_v2.db.models import Target

    target = await session.get(Target, data.target_id)
    if target is None or target.tenant_id != user.tenant_id:
        raise NotFoundError("关联的目标不存在")
    # 护栏：创建即校验目标 URL 是否在授权范围内（越权直接拒绝）
    allowed, reason = check_scope(target, target.url)
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"目标超出授权范围，拒绝创建任务: {reason}",
        )
    task = Task(**data.model_dump())
    task.tenant_id = user.tenant_id
    task.owner_id = user.id
    task.operator = user.email
    task.status = TaskStatus.queued
    session.add(task)
    await session.commit()
    await session.refresh(task)
    # 入队异步执行
    try:
        await enqueue_task(str(task.id))
    except Exception:
        # 队列不可用时回退为 pending，便于后续手动触发
        task.status = TaskStatus.pending
        await session.commit()
    return task


@router.post("/{task_id}/enqueue", response_model=TaskRead)
async def re_enqueue_task(
    task_id: str,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(get_current_user),
) -> Task:
    """将 pending 任务重新入队（队列曾不可用或手动触发）。"""
    task = await session.get(Task, task_id)
    if task is None or task.tenant_id != user.tenant_id:
        raise NotFoundError("任务不存在")
    if task.status not in (TaskStatus.pending, TaskStatus.failed, TaskStatus.cancelled):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"任务当前状态 {task.status.value}，无法重新入队",
        )
    task.status = TaskStatus.queued
    await session.commit()
    await enqueue_task(str(task.id))
    await session.refresh(task)
    return task


@router.get("", response_model=list[TaskRead])
async def list_tasks(
    session: AsyncSession = Depends(get_session),
    user: User = Depends(get_current_user),
    target_id: str | None = Query(default=None),
    status_filter: TaskStatus | None = Query(default=None, alias="status"),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> list[Task]:
    stmt = select(Task).where(Task.tenant_id == user.tenant_id)
    if target_id is not None:
        stmt = stmt.where(Task.target_id == target_id)
    if status_filter is not None:
        stmt = stmt.where(Task.status == status_filter)
    stmt = stmt.order_by(Task.created_at.desc()).limit(limit).offset(offset)
    result = await session.execute(stmt)
    return list(result.scalars().all())


@router.get("/{task_id}", response_model=TaskRead)
async def get_task(
    task_id: str,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(get_current_user),
) -> Task:
    task = await session.get(Task, task_id)
    if task is None or task.tenant_id != user.tenant_id:
        raise NotFoundError("任务不存在")
    return task


@router.patch("/{task_id}", response_model=TaskRead)
async def update_task(
    task_id: str,
    data: TaskUpdate,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(get_current_user),
) -> Task:
    task = await session.get(Task, task_id)
    if task is None or task.tenant_id != user.tenant_id:
        raise NotFoundError("任务不存在")
    for key, value in data.model_dump(exclude_unset=True).items():
        setattr(task, key, value)
    await session.commit()
    await session.refresh(task)
    return task


@router.delete("/{task_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_task(
    task_id: str,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(get_current_user),
) -> None:
    task = await session.get(Task, task_id)
    if task is None or task.tenant_id != user.tenant_id:
        raise NotFoundError("任务不存在")
    await session.delete(task)
    await session.commit()


@router.post("/{task_id}/cancel", response_model=TaskRead)
async def cancel_task(
    task_id: str,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(get_current_user),
) -> Task:
    """请求取消正在运行的任务（协作式取消，Worker 检测到后进入 cancelled）。"""
    task = await session.get(Task, task_id)
    if task is None or task.tenant_id != user.tenant_id:
        raise NotFoundError("任务不存在")
    if task.status not in (TaskStatus.queued, TaskStatus.running):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"任务当前状态 {task.status.value}，无法取消",
        )
    await request_cancel(task.id)
    task.cancel_requested = True
    if task.status == TaskStatus.queued:
        # 尚未开始运行，直接标记 cancelled
        task.status = TaskStatus.cancelled
        task.finished_at = _utcnow()
    await record_audit(
        session, action="task.cancel_requested", outcome="success",
        task_id=task.id, target_id=task.target_id, tenant_id=user.tenant_id,
        actor_id=user.id, actor=user.email,
    )
    await session.commit()
    await session.refresh(task)
    return task


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)
