"""Target CRUD 路由。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from uuid import UUID

from pobi_v2.core.deps import get_current_user, require_scope
from pobi_v2.core.exceptions import ConflictError, NotFoundError
from pobi_v2.db.models import Target, User
from pobi_v2.db.recon_models import ReconEndpointAgg
from pobi_v2.db.session import get_session
from pobi_v2.schemas.target import TargetCreate, TargetRead, TargetUpdate

router = APIRouter(prefix="/api/v1/targets", tags=["targets"])


def _to_orm_payload(data: TargetCreate | TargetUpdate) -> dict:
    payload = data.model_dump(exclude_unset=True)
    # in_scope / out_of_scope 直接以 list 存入 JSON 列
    return payload


@router.post("", response_model=TargetRead, status_code=status.HTTP_201_CREATED)
async def create_target(
    data: TargetCreate,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_scope("targets:write")),
) -> Target:
    try:
        payload = _to_orm_payload(data)
        payload["tenant_id"] = user.tenant_id
        payload["owner_id"] = user.id
        target = Target(**payload)
        session.add(target)
        await session.commit()
        await session.refresh(target)
        return target
    except ConflictError:
        raise
    except Exception as exc:  # 唯一约束等
        await session.rollback()
        if "unique" in str(exc).lower():
            raise ConflictError("该 URL 已存在")
        raise


@router.get("", response_model=list[TargetRead])
async def list_targets(
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_scope("targets:read")),
    enabled: bool | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> list[Target]:
    stmt = select(Target).where(Target.tenant_id == user.tenant_id)
    if enabled is not None:
        stmt = stmt.where(Target.enabled == enabled)
    stmt = stmt.order_by(Target.created_at.desc()).limit(limit).offset(offset)
    result = await session.execute(stmt)
    return list(result.scalars().all())


@router.get("/{target_id}", response_model=TargetRead)
async def get_target(
    target_id: UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_scope("targets:read")),
) -> Target:
    target = await session.get(Target, target_id)
    if target is None or target.tenant_id != user.tenant_id:
        raise NotFoundError("目标不存在")
    return target


@router.get("/{target_id}/assets")
async def list_target_assets(
    target_id: UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_scope("targets:read")),
) -> dict:
    """目标资产/端点全景（per-target 聚合，来自 recon_endpoints_agg）。

    供前端"目标全景图"的资产清单视图：主机 + 路径 + 方法 + 状态码 + 技术栈指纹 +
    参数 + 发现途径 + 时间。跨任务收敛，同一端点跨任务只保留最新状态。
    """
    target = await session.get(Target, target_id)
    if target is None or target.tenant_id != user.tenant_id:
        raise NotFoundError("目标不存在")
    stmt = (
        select(ReconEndpointAgg)
        .where(ReconEndpointAgg.target_id == target_id)
        .order_by(ReconEndpointAgg.host, ReconEndpointAgg.path_normalized)
        .limit(1000)
    )
    result = await session.execute(stmt)
    assets = []
    for r in result.scalars().all():
        assets.append(
            {
                "id": str(r.id),
                "host": r.host,
                "path": r.path_normalized,
                "method": r.method,
                "status_code": r.status_code,
                "auth_required": r.auth_required,
                "tech_stack": r.tech_stack or [],
                "parameters": r.parameters or [],
                "discovered_via": r.discovered_via,
                "confidence": r.confidence,
                "first_seen": r.first_seen.isoformat() if r.first_seen else None,
                "last_seen": r.last_seen.isoformat() if r.last_seen else None,
            }
        )
    return {"target_id": str(target_id), "count": len(assets), "assets": assets}


@router.patch("/{target_id}", response_model=TargetRead)
async def update_target(
    target_id: UUID,
    data: TargetUpdate,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_scope("targets:write")),
) -> Target:
    target = await session.get(Target, target_id)
    if target is None or target.tenant_id != user.tenant_id:
        raise NotFoundError("目标不存在")
    for key, value in _to_orm_payload(data).items():
        setattr(target, key, value)
    await session.commit()
    await session.refresh(target)
    return target


@router.delete("/{target_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_target(
    target_id: UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_scope("targets:write")),
) -> None:
    target = await session.get(Target, target_id, options=[joinedload(Target.tasks)])
    if target is None or target.tenant_id != user.tenant_id:
        raise NotFoundError("目标不存在")
    await session.delete(target)
    await session.commit()
