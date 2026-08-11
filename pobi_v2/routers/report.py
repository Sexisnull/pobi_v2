"""M5 报告路由：生成并导出结构化渗透测试报告。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from fastapi.responses import PlainTextResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from uuid import UUID

from pobi_v2.core.deps import get_current_user
from pobi_v2.core.exceptions import NotFoundError
from pobi_v2.db.models import Artifact, Finding, Task, TaskEvent, User
from pobi_v2.db.session import get_session
from pobi_v2.engine.report import build_report, render_json, render_markdown
from pobi_v2.schemas.persistence import TaskDetailRead

router = APIRouter(prefix="/api/v1/tasks", tags=["report"])


@router.get("/{task_id}/report", response_model=TaskDetailRead)
async def get_report(
    task_id: str,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """返回结构化报告数据（JSON）。"""
    return await _build(task_id, session, user)


@router.get("/{task_id}/report/markdown", response_class=PlainTextResponse)
async def get_report_markdown(
    task_id: str,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(get_current_user),
) -> str:
    """导出 Markdown 报告。"""
    report = await _build(task_id, session, user)
    return render_markdown(report)


@router.get("/{task_id}/report/json", response_class=PlainTextResponse)
async def get_report_json(
    task_id: str,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(get_current_user),
) -> str:
    """导出 JSON 报告。"""
    report = await _build(task_id, session, user)
    return render_json(report)


async def _build(task_id: str, session: AsyncSession, user: User) -> dict:
    task = await session.get(Task, task_id)
    if task is None or task.tenant_id != user.tenant_id:
        raise NotFoundError("任务不存在")
    findings = list(
        (await session.execute(select(Finding).where(Finding.task_id == task.id))).scalars().all()
    )
    events = list(
        (await session.execute(
            select(TaskEvent).where(TaskEvent.task_id == task.id).order_by(TaskEvent.seq)
        )).scalars().all()
    )
    artifacts = list(
        (await session.execute(select(Artifact).where(Artifact.task_id == task.id))).scalars().all()
    )
    return build_report(task, findings, events, artifacts)
