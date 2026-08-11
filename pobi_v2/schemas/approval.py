"""M5 审批相关 Schema。"""
from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel

from pobi_v2.db.models import ApprovalStatus


class ApprovalDecision(BaseModel):
    decision: str  # "approve" | "reject"
    reason: str | None = None


class ApprovalRead(BaseModel):
    id: UUID
    task_id: UUID
    tenant_id: UUID
    tool_name: str
    agent_name: str | None
    tool_args: dict
    status: ApprovalStatus
    decision_reason: str | None
    decided_at: datetime | None
    created_at: datetime

    model_config = {"from_attributes": True}
