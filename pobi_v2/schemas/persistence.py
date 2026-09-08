"""M3 持久化实体的 Pydantic Schema（只读查询用）。"""
from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel

from pobi_v2.db.models import ArtifactKind, Severity, TaskStatus


class TaskEventRead(BaseModel):
    id: UUID
    task_id: UUID
    seq: int
    event_type: str
    payload: dict = {}
    trace_id: str | None = None
    span_id: str | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class TaskEventRangeOut(BaseModel):
    """任务事件的 seq 区间元信息（回放播放器定位用）。

    只含聚合值不含 payload：播放器据此把进度条映射到 seq，再用现有
    ``GET /tasks/{id}/events?after_seq=`` 按窗口滑动取明细。
    """

    task_id: str
    total: int = 0
    min_seq: int | None = None
    max_seq: int | None = None
    first_at: str | None = None
    last_at: str | None = None


class FindingRead(BaseModel):
    id: UUID
    task_id: UUID
    target_id: UUID
    title: str
    severity: Severity
    description: str | None = None
    evidence: dict = {}
    confidence: float | None = None
    cwe: str | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class ArtifactRead(BaseModel):
    id: UUID
    task_id: UUID
    target_id: UUID | None = None
    finding_id: UUID | None = None
    kind: ArtifactKind = ArtifactKind.other
    name: str
    storage_key: str | None = None
    content: str | None = None
    content_type: str | None = None
    size_bytes: int | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class AuditEventRead(BaseModel):
    id: UUID
    task_id: UUID | None
    target_id: UUID | None
    actor_id: UUID | None
    actor: str
    action: str
    outcome: str
    detail: str | None
    meta: dict
    trace_id: str | None
    span_id: str | None
    created_at: datetime

    model_config = {"from_attributes": True}


class TaskDetailRead(BaseModel):
    """任务 + 关联发现 / 产物 / 轨迹的事件计数。"""

    id: UUID
    target_id: UUID
    name: str
    objective: str
    status: TaskStatus
    result: str | None
    error: str | None
    attempts: int
    cancel_requested: bool
    operator: str
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    findings: list[FindingRead] = []
    artifacts: list[ArtifactRead] = []
    event_count: int = 0

    model_config = {"from_attributes": True}
