"""ORM 模型：Target 与 Task。

设计要点：
- Target：被测目标（URL / 范围），对应原 pobi 的 scope/target 概念。
- Task：一次渗透测试任务，归属于 Target，状态机为 M2 任务队列预留。
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum as SAEnum,
    Float,
    ForeignKey,
    Integer,
    JSON,
    String,
    Text,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from pobi_v2.db.session import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class TaskStatus(str, Enum):
    pending = "pending"
    queued = "queued"
    running = "running"
    completed = "completed"
    failed = "failed"
    cancelled = "cancelled"


class Severity(str, Enum):
    info = "info"
    low = "low"
    medium = "medium"
    high = "high"
    critical = "critical"


class ArtifactKind(str, Enum):
    screenshot = "screenshot"
    poc = "poc"
    report = "report"
    log = "log"
    other = "other"


class Tenant(Base):
    """租户（组织）。M4 多租户隔离的根单元。"""

    __tablename__ = "tenants"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    slug: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    users: Mapped[list["User"]] = relationship(
        back_populates="tenant", cascade="all, delete-orphan"
    )


class User(Base):
    """平台用户。M4 起所有资源归属到 user + tenant。"""

    __tablename__ = "users"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    email: Mapped[str] = mapped_column(String(320), nullable=False, unique=True)
    # 显示名（可为空）
    full_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # bcrypt 哈希（前缀 $2b$）
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    tenant: Mapped[Tenant] = relationship(back_populates="users")
    api_tokens: Mapped[list["ApiToken"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class ApiToken(Base):
    """个人访问令牌（PAT），用于脚本 / 第三方直接调用项目 API。

    与登录 JWT 解耦：可设过期时间或长期有效，适合自动化调用。
    - token_hash：明文令牌的 SHA-256，仅用于校验，不可逆。
    - encrypted_secret：用环境变量 POBI_TOKEN_KEY（Fernet）加密后的明文，
      支持前端「点击查看」时按需解密返回，避免以明文落库。
    - prefix：明文令牌的前缀（如 pk_live_），便于列表中识别与展示。
    """

    __tablename__ = "api_tokens"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    tenant_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    prefix: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    encrypted_secret: Mapped[str | None] = mapped_column(Text, nullable=True)
    scopes: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    revoked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    user: Mapped[User] = relationship(back_populates="api_tokens")


class Target(Base):
    __tablename__ = "targets"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    # M4 归属
    tenant_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    owner_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    url: Mapped[str] = mapped_column(String(2048), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 授权范围（白名单），对应原 pobi 的 scope 闸门
    in_scope: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    out_of_scope: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    # 验证策略（Validation Configuration）：决定「怎样才算找到漏洞」
    # flag 正则（捕获即停）、验证格式（如 FLAG{}）、信心阈值带、任务树深度。
    # 运行任务前写入全局 validation.yaml，复用原 ValidationGate(Flag+Judge) 逻辑。
    flag_regex: Mapped[str | None] = mapped_column(String(512), nullable=True)
    validation_format: Mapped[str | None] = mapped_column(String(64), nullable=True)
    confidence_threshold: Mapped[float] = mapped_column(Float, default=0.6, nullable=False)
    max_tree_depth: Mapped[int] = mapped_column(Integer, default=4, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    tasks: Mapped[list["Task"]] = relationship(
        back_populates="target", cascade="all, delete-orphan"
    )


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    target_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("targets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # M4 归属（冗余存储 tenant 以支持跨资源查询与隔离）
    tenant_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    owner_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    objective: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[TaskStatus] = mapped_column(
        SAEnum(TaskStatus), default=TaskStatus.pending, nullable=False, index=True
    )
    # 模型与经济参数（M2 透传给 CoreAgent）
    model: Mapped[str | None] = mapped_column(String(255), nullable=True)
    max_turns: Mapped[int] = mapped_column(default=50)
    # 自主策略模式：hacker=谨慎需人工审批（默认），yolo=自动批准高危工具调用
    agent_mode: Mapped[str] = mapped_column(String(32), default="hacker", nullable=False)
    # 任务种类：task=正式渗透任务，probe=链路连通性探针（仅做授权目标连通验证）
    kind: Mapped[str] = mapped_column(String(32), default="task", nullable=False, index=True)
    # 结果（M3 结构化落库，result 存最终摘要/报告引用）
    result: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 最终置信度（M7：来自 ValidationGate / 报告，0~1）
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    # 取消 / 续跑（M3）
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_agent_session: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # Token 用量（由 CoreAgent.run 的累计 usage 落库；prompt=发送 / completion=接收）
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # 审计
    operator: Mapped[str] = mapped_column(String(128), default="web-operator")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    target: Mapped[Target] = relationship(back_populates="tasks")
    events: Mapped[list["TaskEvent"]] = relationship(
        back_populates="task", cascade="all, delete-orphan", order_by="TaskEvent.seq"
    )
    findings: Mapped[list["Finding"]] = relationship(
        back_populates="task", cascade="all, delete-orphan"
    )
    artifacts: Mapped[list["Artifact"]] = relationship(
        back_populates="task", cascade="all, delete-orphan"
    )


class TaskEvent(Base):
    """一次任务运行产生的有序事件流（思考 / 工具调用 / 状态流转）。

    与 SSE 推送同源，但此处为持久化副本，支持任务结束后回放与审计。
    """

    __tablename__ = "task_events"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    task_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    # 事件负载（JSONB，灵活 schema）
    payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    task: Mapped[Task] = relationship(back_populates="events")


class Finding(Base):
    """渗透测试发现（漏洞 / 风险点）。

    对应原 pobi 的 reporter 结构化输出，M3 起落库以便查询与报告聚合。
    """

    __tablename__ = "findings"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    task_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    target_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("targets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    severity: Mapped[Severity] = mapped_column(
        SAEnum(Severity), default=Severity.info, nullable=False, index=True
    )
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 证据（URL / payload / 响应片段），结构化以便报告复用
    evidence: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    cwe: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    task: Mapped[Task] = relationship(back_populates="findings")
    artifacts: Mapped[list["Artifact"]] = relationship(
        back_populates="finding", cascade="all, delete-orphan"
    )


class Artifact(Base):
    """任务产物（截图 / PoC / 报告 / 日志）。

    M3 起记录元数据并落库；实体内容存对象存储（MinIO），这里仅存引用。
    """

    __tablename__ = "artifacts"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    task_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    finding_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("findings.id", ondelete="CASCADE"), nullable=True, index=True
    )
    kind: Mapped[ArtifactKind] = mapped_column(
        SAEnum(ArtifactKind), default=ArtifactKind.other, nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(512), nullable=False)
    # 对象存储引用（MinIO key / 路径）；文本类产物可直接存 content
    storage_key: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    task: Mapped[Task] = relationship(back_populates="artifacts")
    finding: Mapped[Finding | None] = relationship(back_populates="artifacts")


class ApprovalStatus(str, Enum):
    pending = "pending"
    approved = "approved"
    rejected = "rejected"
    expired = "expired"


class ApprovalRequest(Base):
    """M5 高危操作审批请求。

    Agent 运行中命中高危工具（requires_approval）时，由审批回调创建一条
    pending 请求；操作员通过 API 批准/拒绝后，回调读取决策继续或中止执行。
    """

    __tablename__ = "approval_requests"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    task_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    tenant_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # 请求来源：工具名（如 execute_command）/ 代理名
    tool_name: Mapped[str] = mapped_column(String(128), nullable=False)
    agent_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # 触发该请求的参数（指令/目标），供审批人判断
    tool_args: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[ApprovalStatus] = mapped_column(
        SAEnum(ApprovalStatus), default=ApprovalStatus.pending, nullable=False, index=True
    )
    # 决策信息
    decision_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    decided_by: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class AuditEvent(Base):
    """结构化审计日志（M3 起入库，替代原 web_console 内存 audit_store）。"""

    __tablename__ = "audit_events"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    # 关联实体（可为空，系统级事件）
    task_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True, index=True
    )
    target_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("targets.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # M4 归属
    tenant_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=True, index=True
    )
    actor_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    actor: Mapped[str] = mapped_column(String(128), nullable=False, default="web-operator")
    action: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    # 结果：success / denied / error
    outcome: Mapped[str] = mapped_column(String(32), nullable=False, default="success")
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 扩展字段（IP / 工具名 / 风险等级等）
    meta: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, index=True
    )


class PricingConfig(Base):
    """全局 LLM 价格配置（每百万 token 单价，单位：元/美元由用户自定义）。

    单条记录（id 固定为 DEFAULT_ID），供 Token 用量页估算成本。
    price_input = 输入（prompt）单价；price_output = 输出（completion）单价。
    """

    __tablename__ = "pricing_config"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default="default")
    price_input: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    price_output: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    currency: Mapped[str] = mapped_column(String(8), default="USD", nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )
