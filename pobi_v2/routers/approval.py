"""M5 审批路由：列出与决策高危操作审批请求。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from uuid import UUID

from pobi_v2.core.deps import get_current_user
from pobi_v2.core.exceptions import NotFoundError
from pobi_v2.db.models import ApprovalRequest, ApprovalStatus, Task, User
from pobi_v2.db.session import get_session
from pobi_v2.engine.approval import decide_request
from pobi_v2.schemas.approval import ApprovalDecision, ApprovalRead

router = APIRouter(prefix="/api/v1/approvals", tags=["approvals"])


@router.get("", response_model=list[ApprovalRead])
async def list_approvals(
    status_filter: ApprovalStatus | None = Query(default=None, alias="status"),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
    user: User = Depends(get_current_user),
) -> list[ApprovalRequest]:
    stmt = select(ApprovalRequest).where(ApprovalRequest.tenant_id == user.tenant_id)
    if status_filter is not None:
        stmt = stmt.where(ApprovalRequest.status == status_filter)
    stmt = stmt.order_by(ApprovalRequest.created_at.desc()).limit(limit).offset(offset)
    return list((await session.execute(stmt)).scalars().all())


@router.get("/{request_id}", response_model=ApprovalRead)
async def get_approval(
    request_id: str,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(get_current_user),
) -> ApprovalRequest:
    req = await session.get(ApprovalRequest, request_id)
    if req is None or req.tenant_id != user.tenant_id:
        raise NotFoundError("审批请求不存在")
    return req


@router.post("/{request_id}/decision", response_model=ApprovalRead)
async def decide(
    request_id: str,
    body: ApprovalDecision,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(get_current_user),
) -> ApprovalRequest:
    req = await session.get(ApprovalRequest, request_id)
    if req is None or req.tenant_id != user.tenant_id:
        raise NotFoundError("审批请求不存在")
    # 校验请求所属任务仍存在且属于本租户
    task = await session.get(Task, req.task_id)
    if task is None or task.tenant_id != user.tenant_id:
        raise NotFoundError("关联任务不存在")
    try:
        req = await decide_request(
            session, req.id, body.decision, decided_by=user.id, reason=body.reason
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc))
    await session.commit()
    await session.refresh(req)
    return req
