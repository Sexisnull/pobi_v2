"""Task 的 Pydantic Schema。"""
from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

from pobi_v2.db.models import TaskStatus

# 认证前置模式（PreAuth）
AUTH_MODES = ("none", "auto", "manual")
AUTH_STATUSES = ("none", "pending", "running", "success", "failed", "mfa")


class TaskCreate(BaseModel):
    target_id: UUID
    name: str = Field(..., min_length=1, max_length=255)
    objective: str = Field(..., min_length=1)
    model: str | None = None
    max_turns: int = 50
    agent_mode: str = Field(default="hacker", pattern="^(hacker|yolo)$")
    operator: str = "web-operator"
    kind: str = "task"
    # 是否靶场（CTF / 夺旗）：勾选则必须配置 flag_regex 供验证 Agent 验收
    is_range: bool = False
    # 验证策略（任务级覆盖；None = 继承授权目标配置/使用默认）
    flag_regex: str | None = Field(default=None, max_length=512)
    validation_format: str | None = Field(default=None, max_length=64)
    confidence_threshold: float | None = Field(default=None, ge=0.0, le=1.0)
    max_tree_depth: int | None = Field(default=None, ge=1, le=16)
    # 认证前置（PreAuth）：任务创建阶段完成登录，为 L0 认证后爬取提供会话
    auth_mode: str = Field(default="none", pattern="^(none|auto|manual)$")
    auth_username: str | None = Field(default=None, max_length=255)
    # 明文密码仅存在于创建请求，落库前用 Fernet 加密，TaskRead 不回读
    auth_password: str | None = Field(default=None, max_length=4096)
    auth_login_url: str | None = Field(default=None, max_length=2048)
    auth_profile: str = Field(default="preauth", max_length=64)

    @model_validator(mode="after")
    def _check_auth_config(self) -> "TaskCreate":
        if self.is_range and not self.flag_regex:
            raise ValueError("靶场（is_range）任务必须配置 flag_regex 供验证 Agent 验收")
        if self.auth_mode == "auto" and not (self.auth_username and self.auth_password):
            raise ValueError("账号密码自动认证（auth_mode=auto）必须提供 auth_username 与 auth_password")
        return self


class TaskUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=255)
    objective: str | None = None
    status: TaskStatus | None = None
    model: str | None = None
    max_turns: int | None = None
    agent_mode: str | None = Field(default=None, pattern="^(hacker|yolo)$")
    is_range: bool | None = None
    # 验证策略（任务级覆盖；None 不修改，需清空时显式传空字符串）
    flag_regex: str | None = Field(default=None, max_length=512)
    validation_format: str | None = Field(default=None, max_length=64)
    confidence_threshold: float | None = Field(default=None, ge=0.0, le=1.0)
    max_tree_depth: int | None = Field(default=None, ge=1, le=16)
    # 认证前置（PreAuth）更新：密码仅在变更凭据时提交，落库前加密
    auth_mode: str | None = Field(default=None, pattern="^(none|auto|manual)$")
    auth_username: str | None = Field(default=None, max_length=255)
    auth_password: str | None = Field(default=None, max_length=4096)
    auth_login_url: str | None = Field(default=None, max_length=2048)
    auth_profile: str | None = Field(default=None, max_length=64)

    @model_validator(mode="after")
    def _check_range_flag_update(self) -> "TaskUpdate":
        # 显式声明为靶场却未配 flag_regex 时拦截
        if self.is_range is True and not self.flag_regex:
            raise ValueError("靶场（is_range）任务必须配置 flag_regex 供验证 Agent 验收")
        if self.auth_mode == "auto" and not (self.auth_username and self.auth_password):
            raise ValueError("账号密码自动认证（auth_mode=auto）必须提供 auth_username 与 auth_password")
        return self


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
    is_range: bool = False
    # 验证策略（任务级；None 表示未覆盖，运行时继承授权目标配置/使用默认）
    flag_regex: str | None = None
    validation_format: str | None = None
    confidence_threshold: float | None = None
    max_tree_depth: int | None = None
    # 认证前置（PreAuth）：回读配置与状态，不回读加密凭据
    auth_mode: str = "none"
    auth_status: str = "none"
    auth_username: str | None = None
    auth_login_url: str | None = None
    auth_profile: str = "preauth"
    auth_error: str | None = None
    auth_updated_at: datetime | None = None
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

