"""运行指令路由：用户向运行中的主控 Agent 追加指令（真正干预运行）。

POST /api/v1/tasks/{task_id}/instructions
  - 校验任务存在且属于当前租户
  - 校验任务处于 running 状态（否则 409，与 cancel 校验一致）
  - 写入 per-task pending 指令队列（engine/instruction_channel），由 Worker 检查点消费
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from uuid import UUID

from pobi_v2.core.deps import get_current_user, require_scope
from pobi_v2.core.exceptions import NotFoundError
from pobi_v2.db.models import Task, TaskStatus, User
from pobi_v2.db.session import get_session
from pobi_v2.schemas.task import TaskInstructionIn
from pobi_v2.engine.instruction_channel import queue_instruction

router = APIRouter(prefix="/api/v1/tasks", tags=["instructions"])


@router.post("/{task_id}/instructions", status_code=status.HTTP_202_ACCEPTED)
async def post_instruction(
    task_id: UUID,
    data: TaskInstructionIn,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_scope("tasks:write")),
):
    task = await session.get(Task, task_id)
    if task is None or task.tenant_id != user.tenant_id:
        raise NotFoundError("任务不存在")
    if task.status != TaskStatus.running:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"任务当前状态 {task.status.value}，仅运行中任务可追加指令",
        )
    await queue_instruction(
        str(task.id),
        data.instruction,
        meta={"created_by": user.email, "tenant_id": str(user.tenant_id)},
    )
    return {"accepted": True, "task_id": str(task.id)}
