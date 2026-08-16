"""Task CRUD 路由（含 M2 异步执行触发）。

创建任务时：校验授权范围 -> 状态置 queued -> 入队 ARQ，由 Worker 异步执行并推流。
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from uuid import UUID

from pobi_v2.core.deps import get_current_user, require_scope
from pobi_v2.core.exceptions import NotFoundError
from pobi_v2.db.models import Task, TaskEvent, TaskStatus, Target, User
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
    EventReplay,
    TaskEventRead,
)
from pobi_v2.engine.queue import enqueue_task
from pobi_v2.engine.guardrails import check_scope
from pobi_v2.engine.cancel_state import request_cancel

router = APIRouter(prefix="/api/v1/tasks", tags=["tasks"])


@router.post("", response_model=TaskRead, status_code=status.HTTP_201_CREATED)
async def create_task(
    data: TaskCreate,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_scope("tasks:write")),
) -> Task:
    # 校验 target 存在且属于当前租户
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
    task_id: UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_scope("tasks:write")),
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
    user: User = Depends(require_scope("tasks:read")),
    target_id: UUID | None = Query(default=None),
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
    user: User = Depends(require_scope("tasks:read")),
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
    task_id: UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_scope("tasks:read")),
) -> Task:
    task = await session.get(Task, task_id)
    if task is None or task.tenant_id != user.tenant_id:
        raise NotFoundError("任务不存在")
    return task


@router.get("/{task_id}/usage", response_model=TaskUsage)
async def task_usage(
    task_id: UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_scope("tasks:read")),
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
    task_id: UUID,
    data: TaskUpdate,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_scope("tasks:write")),
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
    task_id: UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_scope("tasks:write")),
) -> None:
    task = await session.get(Task, task_id)
    if task is None or task.tenant_id != user.tenant_id:
        raise NotFoundError("任务不存在")
    await session.delete(task)
    await session.commit()


@router.post("/{task_id}/cancel", response_model=TaskRead)
async def cancel_task(
    task_id: UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_scope("tasks:write")),
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

    # 若任务正在 ARQ 队列/执行中，尝试从队列移除该 job：
    # 1) 立即使 worker-status 的 queue_depth 回落（报告 C 的陈旧统计问题）；
    # 2) 为 task-reconcile 对账提供「队列中已不存在」依据，及时回收幽灵任务。
    # abort_job 仅移除调度项，已运行的协程由 executor 的子超时 + is_cancelled 兜底终止。
    if task.status == TaskStatus.running:
        try:
            from pobi_v2.engine.queue import get_redis

            redis = await get_redis()
            try:
                # ARQ 的 abort_job 通过 job_id（本服务以 task_id 对齐）标记中止，
                # 并从 arq:queue / arq:in_progress 移除，使队列深度即时下降。
                await redis.zrem("arq:queue", str(task.id))
                await redis.zrem("arq:in_progress", str(task.id))
            finally:
                await redis.aclose()
        except Exception:  # noqa: BLE001 — 队列清理失败不影响取消请求的记录
            pass

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


async def _load_or_404(session: AsyncSession, task_id: UUID, tenant_id) -> Task:
    task = await session.get(Task, task_id)
    if task is None or task.tenant_id != tenant_id:
        raise NotFoundError("任务不存在")
    return task


@router.get("/{task_id}/plan", response_model=PlanSummary)
async def get_task_plan(
    task_id: UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_scope("tasks:read")),
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
    task_id: UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_scope("tasks:read")),
):
    """任务实时状态聚合：供控制台中栏顶部与运行视图即时渲染。"""
    task = await session.get(Task, task_id, options=[selectinload(Task.target)])
    if task is None or task.tenant_id != user.tenant_id:
        raise NotFoundError("任务不存在")

    # 混合事件窗口：按类型分组各保留最近 N 条，再合并取全局最近 60 条，
    # 避免高频骨架/明细事件把关键事件挤出固定 30 条窗口（报告 D 暴露的缺口）。
    ev_rows = await session.execute(
        select(TaskEvent)
        .where(TaskEvent.task_id == task_id)
        .order_by(TaskEvent.seq.desc())
        .limit(400)
    )
    all_ev = list(reversed(ev_rows.scalars().all()))
    _PER_TYPE = 12
    _MIXED_CAP = 60
    buckets: dict[str, list] = {}
    for ev in all_ev:
        buckets.setdefault(ev.event_type, []).append(ev)
    mixed: list = []
    for evs in buckets.values():
        mixed.extend(evs[-_PER_TYPE:])
    mixed.sort(key=lambda e: e.seq)
    recent_events = [
        {
            "seq": ev.seq,
            "type": ev.event_type,
            "payload": ev.payload,
            "created_at": ev.created_at.isoformat() if ev.created_at else None,
        }
        for ev in mixed[-_MIXED_CAP:]
    ]
    last_event_at = all_ev[-1].created_at.isoformat() if all_ev else None

    current_phase = None
    current_agent = None
    # 运行视图：从全量窗口取当前阶段（phase_changed 低频，不会被淹没）
    for ev in all_ev:
        p = ev.payload or {}
        if ev.event_type == "phase_changed" and p.get("new_phase"):
            current_phase = p["new_phase"]

    # 运行视图：独立查询 agent_start/agent_end，避免被 plan_step 等高频事件淹没
    # （recent 30 条窗口只用于事件流展示，agents 聚合须走全量查询）
    agent_rows = await session.execute(
        select(TaskEvent)
        .where(
            TaskEvent.task_id == task_id,
            TaskEvent.event_type.in_(["agent_start", "agent_end"]),
        )
        .order_by(TaskEvent.created_at.asc())
    )
    agents: dict[str, "AgentRuntime"] = {}
    for ev in agent_rows.scalars().all():
        p = ev.payload or {}
        name = p.get("agent_name")
        if not name:
            continue
        if ev.event_type == "agent_start":
            current_agent = name
            agents.setdefault(
                name,
                AgentRuntime(
                    name=name,
                    role=p.get("role", "agent"),
                    status="running",
                    last_event_at=ev.created_at.isoformat() if ev.created_at else None,
                ),
            )
            agents[name].status = "running"
            agents[name].last_event_at = ev.created_at.isoformat() if ev.created_at else None
        elif ev.event_type == "agent_end" and name in agents:
            agents[name].status = "done"
            agents[name].last_event_at = ev.created_at.isoformat() if ev.created_at else None

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

    # 运行视图：聚合每个 agent 正在/最近执行的工具调用（"在做什么"）
    tool_rows = await session.execute(
        select(TaskEvent)
        .where(
            TaskEvent.task_id == task_id,
            TaskEvent.event_type.in_(["tool_call_start", "tool_call_end"]),
        )
        .order_by(TaskEvent.created_at.desc())
        .limit(200)
    )
    agent_work: dict[str, list[dict]] = {}
    for ev in reversed(tool_rows.scalars().all()):
        p = ev.payload or {}
        agent_name = p.get("agent_name") or "unknown"
        entry: dict = {"tool": p.get("tool_name", "?"), "ts": ev.created_at.isoformat() if ev.created_at else None}
        if ev.event_type == "tool_call_start":
            entry["args"] = (p.get("args") or "")[:2000]
        else:
            entry["success"] = bool(p.get("success"))
            if p.get("error"):
                entry["error"] = str(p.get("error"))[:2000]
            entry["result"] = (p.get("result") or "")[:2000]
        agent_work.setdefault(agent_name, []).append(entry)

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
        agent_work=agent_work,
        last_event_at=last_event_at,
    )


@router.get("/{task_id}/events", response_model=EventReplay)
async def get_task_events(
    task_id: UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_scope("tasks:read")),
    type_filter: str | None = Query(default=None, alias="type"),
    after_seq: int | None = Query(default=None, alias="after_seq"),
    limit: int = Query(default=100, ge=1, le=500),
):
    """事件回放：从 TaskEvent 表读取历史全量（弥补 SSE 断连即丢的缺陷）。

    支持按事件类型过滤、按 seq 游标分页，供控制台时间线回看 agent 思考/工具/错误全过程。
    """
    task = await session.get(Task, task_id)
    if task is None or task.tenant_id != user.tenant_id:
        raise NotFoundError("任务不存在")

    total_stmt = select(func.count(TaskEvent.id)).where(TaskEvent.task_id == task_id)
    if type_filter:
        total_stmt = total_stmt.where(TaskEvent.event_type == type_filter)
    total = (await session.execute(total_stmt)).scalar() or 0

    page_stmt = select(TaskEvent).where(TaskEvent.task_id == task_id)
    if type_filter:
        page_stmt = page_stmt.where(TaskEvent.event_type == type_filter)
    if after_seq is not None:
        page_stmt = page_stmt.where(TaskEvent.seq > after_seq)
    page_stmt = page_stmt.order_by(TaskEvent.seq.asc()).limit(limit)
    rows = (await session.execute(page_stmt)).scalars().all()
    events = [
        TaskEventRead(
            seq=ev.seq,
            type=ev.event_type,
            payload=ev.payload,
            created_at=ev.created_at.isoformat() if ev.created_at else None,
        )
        for ev in rows
    ]
    next_after = events[-1].seq if len(events) == limit and events else None
    return EventReplay(events=events, total=int(total), next_after_seq=next_after)
