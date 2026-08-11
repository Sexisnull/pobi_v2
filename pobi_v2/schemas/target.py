"""Target 的 Pydantic Schema。"""
from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class ScopeList(BaseModel):
    in_scope: list[str] = Field(default_factory=list)
    out_of_scope: list[str] = Field(default_factory=list)


class TargetCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    url: str = Field(..., min_length=1, max_length=2048)
    description: str | None = None
    in_scope: list[str] = Field(default_factory=list)
    out_of_scope: list[str] = Field(default_factory=list)
    enabled: bool = True


class TargetUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=255)
    url: str | None = Field(default=None, max_length=2048)
    description: str | None = None
    in_scope: list[str] | None = None
    out_of_scope: list[str] | None = None
    enabled: bool | None = None


class TargetRead(BaseModel):
    id: UUID
    name: str
    url: str
    description: str | None
    in_scope: list[str]
    out_of_scope: list[str]
    enabled: bool
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}
