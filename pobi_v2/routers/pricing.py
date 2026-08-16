"""LLM 价格配置路由：用户自定义每百万 token 单价（供 Token 用量页估算成本）。

单条全局配置（id 固定为 "default"），GET 读取、PUT 更新。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from pobi_v2.core.deps import get_current_user, require_scope
from pobi_v2.db.models import PricingConfig, User
from pobi_v2.db.session import get_session
from pobi_v2.schemas.pricing import PricingConfigRead, PricingConfigUpdate

_PRICING_ID = "default"

router = APIRouter(prefix="/api/v1/pricing", tags=["pricing"])


async def _get_or_create(session: AsyncSession) -> PricingConfig:
    cfg = await session.get(PricingConfig, _PRICING_ID)
    if cfg is None:
        cfg = PricingConfig(
            id=_PRICING_ID, price_input=0.0, price_output=0.0, currency="USD"
        )
        session.add(cfg)
        await session.commit()
        await session.refresh(cfg)
    return cfg


@router.get("", response_model=PricingConfigRead)
async def get_pricing(
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_scope("pricing:read")),
) -> PricingConfig:
    return await _get_or_create(session)


@router.put("", response_model=PricingConfigRead)
async def update_pricing(
    data: PricingConfigUpdate,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_scope("pricing:write")),
) -> PricingConfig:
    cfg = await _get_or_create(session)
    cfg.price_input = data.price_input
    cfg.price_output = data.price_output
    cfg.currency = data.currency
    await session.commit()
    await session.refresh(cfg)
    return cfg
