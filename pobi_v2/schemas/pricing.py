"""LLM 价格配置 Schema（每百万 token 单价，供 Token 用量页估算成本）。"""
from __future__ import annotations

from pydantic import BaseModel, Field


class PricingConfigRead(BaseModel):
    price_input: float = 0.0
    price_output: float = 0.0
    currency: str = "USD"

    model_config = {"from_attributes": True}


class PricingConfigUpdate(BaseModel):
    price_input: float = Field(default=0.0, ge=0)
    price_output: float = Field(default=0.0, ge=0)
    currency: str = Field(default="USD", max_length=8)
