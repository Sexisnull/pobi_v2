"""M5 报告路由：生成并导出结构化渗透测试报告。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from fastapi.responses import PlainTextResponse, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from uuid import UUID

from pobi_v2.core.deps import get_current_user, require_scope
from pobi_v2.core.exceptions import NotFoundError
from pobi_v2.db.models import Artifact, ArtifactKind, Finding, Task, TaskEvent, User
from pobi_v2.db.session import get_session
from pobi_v2.engine.report import build_report, render_json, render_markdown
from pobi_v2.schemas.persistence import TaskDetailRead

router = APIRouter(prefix="/api/v1", tags=["report"])


@router.get("/tasks/{task_id}/report", response_model=None)
async def get_report(
    task_id: UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_scope("tasks:read")),
):
    """返回结构化报告数据（JSON）。"""
    return await _build(task_id, session, user)


@router.get("/tasks/{task_id}/report/markdown", response_class=PlainTextResponse)
async def get_report_markdown(
    task_id: UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_scope("tasks:read")),
) -> str:
    """导出 Markdown 报告（含完整报告正文，如果存在 report 类型 artifact）。"""
    report = await _build(task_id, session, user)
    md = render_markdown(report)
    # 如果存在 report 类型 artifact 且有 content，追加完整报告正文
    artifacts = report.get("artifacts", [])
    for a in artifacts:
        if a.get("kind") == "report" and a.get("content"):
            md += "\n\n---\n\n## 完整报告正文\n\n" + a["content"]
            break
    return md


@router.get("/tasks/{task_id}/report/json", response_class=PlainTextResponse)
async def get_report_json(
    task_id: UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_scope("tasks:read")),
) -> str:
    """导出 JSON 报告。"""
    report = await _build(task_id, session, user)
    return render_json(report)


@router.get("/artifacts/{artifact_id}/download")
async def download_artifact(
    artifact_id: UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_scope("tasks:read")),
):
    """下载 artifact 内容（报告 / PoC / 截图等）。"""
    artifact = await session.get(Artifact, artifact_id)
    if artifact is None:
        raise NotFoundError("产物不存在")
    # 校验租户权限
    task = await session.get(Task, artifact.task_id)
    if task is None or task.tenant_id != user.tenant_id:
        raise NotFoundError("产物不存在")

    content = artifact.content or ""
    content_type = artifact.content_type or "application/octet-stream"
    filename = artifact.name or f"artifact-{artifact_id}"
    # 确保文件名安全
    safe_filename = filename.replace("/", "_").replace("\\", "_")
    # RFC 5987: UTF-8 文件名编码（支持中文）
    from urllib.parse import quote
    encoded_filename = quote(safe_filename)
    headers = {
        "Content-Disposition": f"attachment; filename*=UTF-8''{encoded_filename}",
    }
    return Response(content=content, media_type=content_type, headers=headers)


async def _build(task_id: UUID, session: AsyncSession, user: User) -> dict:
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
    report = build_report(task, findings, events, artifacts)
    # 在 artifact 列表中包含 content（用于 markdown 报告渲染和下载）
    for i, a in enumerate(artifacts):
        if i < len(report.get("artifacts", [])):
            report["artifacts"][i]["content"] = a.content
    return report
