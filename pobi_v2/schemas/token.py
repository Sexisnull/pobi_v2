"""API 令牌（PAT）相关 schema。"""
from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field


class ApiTokenCreate(BaseModel):
    """创建令牌请求。"""

    name: str = Field(..., min_length=1, max_length=255)
    expires_in_days: int | None = Field(
        default=None, ge=1, le=3650, description="有效期天数；为空表示长期有效"
    )
    scopes: list[str] = Field(default_factory=list, description="权限范围；空表示全量")


class ApiTokenRead(BaseModel):
    """令牌列表 / 详情（不含明文）。"""

    id: UUID
    name: str
    prefix: str
    scopes: list[str]
    expires_at: datetime | None
    last_used_at: datetime | None
    revoked: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class ApiTokenCreated(ApiTokenRead):
    """创建成功响应，含一次性 / 可查看的明文令牌。"""

    plaintext_token: str
    # 是否可点击查看（后端保留加密副本时为真）
    revealable: bool = True


class ApiTokenReveal(BaseModel):
    """点击查看返回的明文令牌。"""

    id: UUID
    plaintext_token: str
