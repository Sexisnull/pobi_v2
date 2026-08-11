"""Task 的 Pydantic Schema。"""
from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field

from pobi_v2.db.models import TaskStatus


class TaskCreate(BaseModel):
    target_id: UUID
    name: str = Field(..., min_length=1, max_length=255)
    objective: str = Field(..., min_length=1)
    model: str | None = None
    max_turns: int = 50
    operator: str = "web-operator"


class TaskUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=255)
    objective: str | None = None
    status: TaskStatus | None = None
    model: str | None = None
    max_turns: int | None = None


class TaskRead(BaseModel):
    id: UUID
    target_id: UUID
    name: str
    objective: str
    status: TaskStatus
    model: str | None
    max_turns: int
    result: str | None
    error: str | None
    operator: str
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    finished_at: datetime | None

    model_config = {"from_attributes": True}
