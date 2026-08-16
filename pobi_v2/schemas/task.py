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
    kind: str = "task"


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
    kind: str = "task"
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    # Token 用量（发送=prompt_tokens / 接收=completion_tokens / 总计=total_tokens）
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

    model_config = {"from_attributes": True}


class TaskUsage(BaseModel):
    """单次任务的 token 用量明细。"""
    task_id: str
    name: str
    status: str
    model: str | None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

    model_config = {"from_attributes": True}


class UsageSummary(BaseModel):
    """全部任务 token 用量汇总。"""
    task_count: int = 0
    total_prompt_tokens: int = 0
    total_completion_tokens: int = 0
    total_tokens: int = 0
    # 按状态拆分（仅统计有意义的消耗）
    completed_prompt_tokens: int = 0
    completed_completion_tokens: int = 0
    completed_total_tokens: int = 0


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
    agent_work: dict[str, list[dict]] = Field(default_factory=dict)
    # 派生字段：最近一条事件的时间，前端据此显示『最后活跃 Xs 前』，
    # 避免长时间任务 updated_at 因节流更新而看似静止。
    last_event_at: str | None = None


class TaskEventRead(BaseModel):
    """单条持久化任务事件（供 /events 回放接口）。"""

    seq: int
    type: str
    payload: dict
    created_at: str | None = None


class EventReplay(BaseModel):
    """事件回放分页结果（供控制台时间线回看，弥补 SSE 断连即丢的缺陷）。"""

    events: list[TaskEventRead] = []
    total: int = 0
    next_after_seq: int | None = None


class TaskInstructionIn(BaseModel):
    """用户向主控 Agent 追加的指令。"""

    instruction: str = Field(..., min_length=1, max_length=2000)


class ProbeRequest(BaseModel):
    """端到端链路探针请求：派发一个轻量 agent 任务，在共享 Kali 沙箱访问已授权目标。

    仅做连通性验证（如用 curl 访问目标首页并报告 HTTP 状态码），不深入利用。
    """

    target_id: UUID
    prompt: str | None = None
    max_turns: int = 8


class ProbeResponse(BaseModel):
    """端到端链路探针响应：返回被派发的探针任务 id，供后续用任务查询 / SSE 观测完整链路。"""

    task_id: UUID
    target_id: UUID
    status: str
    message: str

