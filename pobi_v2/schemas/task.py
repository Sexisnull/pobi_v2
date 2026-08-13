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
    agent_mode: str = Field(default="hacker", pattern="^(hacker|yolo)$")
    operator: str = "web-operator"


class TaskUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=255)
    objective: str | None = None
    status: TaskStatus | None = None
    model: str | None = None
    max_turns: int | None = None
    agent_mode: str | None = Field(default=None, pattern="^(hacker|yolo)$")


class TaskRead(BaseModel):
    id: UUID
    target_id: UUID
    name: str
    objective: str
    status: TaskStatus
    model: str | None
    max_turns: int
    agent_mode: str
    result: str | None
    error: str | None
    operator: str
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    finished_at: datetime | None

    model_config = {"from_attributes": True}


class PlanStep(BaseModel):
    """结构化执行计划中的一个步骤（由引擎在威胁建模/利用阶段拆解发出）。"""

    step_id: str
    seq: int
    title: str
    status: str = Field(..., pattern="^(pending|running|completed|failed)$")
    detail: str | None = None


class PlanSummary(BaseModel):
    """执行计划聚合：步骤列表 + 概览计数。"""

    steps: list[PlanStep] = []
    total: int = 0
    completed: int = 0
    running: int = 0
    failed: int = 0


class AgentRuntime(BaseModel):
    """参与任务的智能体及其运行态。"""

    name: str
    role: str
    status: str = Field(..., pattern="^(idle|running|done|error)$")
    last_event_at: str | None = None


class TaskLiveState(BaseModel):
    """任务实时状态聚合（控制台中栏顶部 / 全局）。"""

    status: str
    current_phase: str | None = None
    current_agent: str | None = None
    agent_mode: str | None = None
    objective: str | None = None
    target_url: str | None = None
    agents: list[AgentRuntime] = []
    plan: PlanSummary = PlanSummary()
    pending_instructions: int = 0
    recent_events: list[dict] = []


class TaskInstructionIn(BaseModel):
    """用户向主控 Agent 追加的指令。"""

    instruction: str = Field(..., min_length=1, max_length=2000)

