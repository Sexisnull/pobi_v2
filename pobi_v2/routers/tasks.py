"""Task CRUD 路由（含 M2 异步执行触发）。

创建任务时：校验授权范围 -> 状态置 queued -> 入队 ARQ，由 Worker 异步执行并推流。
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from pobi_v2.core.deps import get_current_user
from pobi_v2.core.exceptions import NotFoundError
from pobi_v2.db.models import Task, TaskEvent, TaskStatus, User
from pobi_v2.db.session import get_session
from pobi_v2.db.persistence import record_audit
from pobi_v2.schemas.task import (
    TaskCreate,
    TaskRead,
    TaskUpdate,
    TaskUsage,
    UsageSummary,
    PlanStep,
    PlanSummary,
    AgentRuntime,
    TaskLiveState,
)
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


@router.get("/usage/summary", response_model=UsageSummary)
async def usage_summary(
    session: AsyncSession = Depends(get_session),
    user: User = Depends(get_current_user),
) -> UsageSummary:
    """全部任务的 token 用量汇总（发送 / 接收 / 总计，以及已完成任务拆分）。"""
    rows = await session.execute(
        select(
            func.count(Task.id),
            func.coalesce(func.sum(Task.prompt_tokens), 0),
            func.coalesce(func.sum(Task.completion_tokens), 0),
            func.coalesce(func.sum(Task.total_tokens), 0),
        ).where(Task.tenant_id == user.tenant_id)
    )
    count, sum_prompt, sum_completion, sum_total = rows.first()
    # 已完成任务单独统计
    comp_rows = await session.execute(
        select(
            func.coalesce(func.sum(Task.prompt_tokens), 0),
            func.coalesce(func.sum(Task.completion_tokens), 0),
            func.coalesce(func.sum(Task.total_tokens), 0),
        ).where(
            Task.tenant_id == user.tenant_id, Task.status == TaskStatus.completed
        )
    )
    comp_prompt, comp_completion, comp_total = comp_rows.first()
    return UsageSummary(
        task_count=int(count or 0),
        total_prompt_tokens=int(sum_prompt or 0),
        total_completion_tokens=int(sum_completion or 0),
        total_tokens=int(sum_total or 0),
        completed_prompt_tokens=int(comp_prompt or 0),
        completed_completion_tokens=int(comp_completion or 0),
        completed_total_tokens=int(comp_total or 0),
    )


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


@router.get("/{task_id}/usage", response_model=TaskUsage)
async def task_usage(
    task_id: str,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(get_current_user),
) -> TaskUsage:
    """单次任务的 token 用量明细（发送 / 接收 / 总计）。"""
    task = await session.get(Task, task_id)
    if task is None or task.tenant_id != user.tenant_id:
        raise NotFoundError("任务不存在")
    return TaskUsage(
        task_id=str(task.id),
        name=task.name,
        status=task.status.value if hasattr(task.status, "value") else str(task.status),
        model=task.model,
        prompt_tokens=task.prompt_tokens,
        completion_tokens=task.completion_tokens,
        total_tokens=task.total_tokens,
    )


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


async def _load_or_404(session: AsyncSession, task_id: str, tenant_id) -> Task:
    task = await session.get(Task, task_id)
    if task is None or task.tenant_id != tenant_id:
        raise NotFoundError("任务不存在")
    return task


@router.get("/{task_id}/plan", response_model=PlanSummary)
async def get_task_plan(
    task_id: str,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """结构化执行计划：聚合 persisted plan_step 事件，按 seq 还原步骤顺序与状态。"""
    task = await session.get(Task, task_id)
    if task is None or task.tenant_id != user.tenant_id:
        raise NotFoundError("任务不存在")

    rows = await session.execute(
        select(TaskEvent)
        .where(TaskEvent.task_id == task_id, TaskEvent.event_type == "plan_step")
        .order_by(TaskEvent.seq.asc(), TaskEvent.created_at.asc())
    )
    events = rows.scalars().all()
    by_step: dict[str, "PlanStep"] = {}
    order: list[str] = []
    for ev in events:
        p = ev.payload or {}
        sid = p.get("step_id")
        if not sid:
            continue
        prev = by_step.get(sid)
        step = PlanStep(
            step_id=sid,
            seq=p.get("seq", 0) if p.get("seq", -1) >= 0 else (prev.seq if prev else 0),
            title=p.get("title", ""),
            status=p.get("status", "pending"),
            detail=p.get("detail"),
        )
        if sid not in by_step:
            order.append(sid)
        by_step[sid] = step
    steps = [by_step[s] for s in order]
    return PlanSummary(
        steps=steps,
        total=len(steps),
        completed=sum(1 for s in steps if s.status == "completed"),
        running=sum(1 for s in steps if s.status == "running"),
        failed=sum(1 for s in steps if s.status == "failed"),
    )


@router.get("/{task_id}/live", response_model=TaskLiveState)
async def get_task_live(
    task_id: str,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """任务实时状态聚合：供控制台中栏顶部与运行视图即时渲染。"""
    task = await session.get(Task, task_id, options=[selectinload(Task.target)])
    if task is None or task.tenant_id != user.tenant_id:
        raise NotFoundError("任务不存在")

    ev_rows = await session.execute(
        select(TaskEvent)
        .where(TaskEvent.task_id == task_id)
        .order_by(TaskEvent.seq.desc())
        .limit(30)
    )
    ev_objs = list(reversed(ev_rows.scalars().all()))
    recent_events = [
        {
            "seq": ev.seq,
            "type": ev.event_type,
            "payload": ev.payload,
            "created_at": ev.created_at.isoformat() if ev.created_at else None,
        }
        for ev in ev_objs
    ]

    current_phase = None
    current_agent = None
    agents: dict[str, "AgentRuntime"] = {}
    for ev in ev_objs:
        p = ev.payload or {}
        if ev.event_type == "phase_changed" and p.get("new_phase"):
            current_phase = p["new_phase"]
        if ev.event_type == "agent_start" and p.get("agent_name"):
            current_agent = p["agent_name"]
            agents.setdefault(
                p["agent_name"],
                AgentRuntime(
                    name=p["agent_name"],
                    role=p.get("role", "agent"),
                    status="running",
                    last_event_at=ev.created_at.isoformat() if ev.created_at else None,
                ),
            )
        if ev.event_type == "agent_end" and p.get("agent_name") and p["agent_name"] in agents:
            agents[p["agent_name"]].status = "done"

    # 执行计划概览
    plan_rows = await session.execute(
        select(TaskEvent)
        .where(TaskEvent.task_id == task_id, TaskEvent.event_type == "plan_step")
        .order_by(TaskEvent.seq.asc(), TaskEvent.created_at.asc())
    )
    seen: dict[str, "PlanStep"] = {}
    for ev in plan_rows.scalars().all():
        p = ev.payload or {}
        sid = p.get("step_id")
        if not sid:
            continue
        seen[sid] = PlanStep(
            step_id=sid,
            seq=p.get("seq", 0),
            title=p.get("title", ""),
            status=p.get("status", "pending"),
            detail=p.get("detail"),
        )
    plan = PlanSummary(
        steps=list(seen.values()),
        total=len(seen),
        completed=sum(1 for s in seen.values() if s.status == "completed"),
        running=sum(1 for s in seen.values() if s.status == "running"),
        failed=sum(1 for s in seen.values() if s.status == "failed"),
    )

    from pobi_v2.engine.instruction_channel import peek_instructions

    pending = await peek_instructions(str(task_id))
    target_url = getattr(task.target, "url", None) if getattr(task, "target", None) else None

    return TaskLiveState(
        status=task.status.value if hasattr(task.status, "value") else str(task.status),
        current_phase=current_phase,
        current_agent=current_agent,
        agent_mode=task.agent_mode,
        objective=task.objective,
        target_url=target_url,
        agents=list(agents.values()),
        plan=plan,
        pending_instructions=len(pending),
        recent_events=recent_events,
    )
