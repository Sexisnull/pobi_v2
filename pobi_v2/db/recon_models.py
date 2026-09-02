"""RECON PG 聚合层模型（设计文档 §5，第二阶）。

把 per-task 本地 SQLite 侦察资产，以 ``target_id``（授权目标 UUID）为维度聚合进
PostgreSQL，作为跨任务记忆层与续扫基线源。本地库每次落库变更经事件钩子异步
增量同步回 PG（upsert 收敛），任务启动时再从 PG 拉取目标历史沉淀灌入新建本地库。

维度约定（v2.1）：
- 本地库 per-task（每任务一个 .db），PG 聚合层 per-target（跨任务收敛）。
- ``target_id: UUID`` 对齐 ``Task.target_id``；``tenant_id`` 冗余以支持多租户隔离查询。
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    JSON,
    LargeBinary,
    String,
    Text,
    Uuid,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from pobi_v2.db.session import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ReconAggThreatStatus(str, Enum):
    """聚合层威胁确认状态（与本地库 ThreatStatus 对应，收敛为 PG 枚举）。"""

    suspected = "suspected"
    confirmed = "confirmed"
    exploited = "exploited"
    remediated = "remediated"


class ReconFactAgg(Base):
    """recon_facts_agg：按 (target_id, category, key) 聚合的事实宽表。

    唯一约束保证跨任务 upsert 收敛——同一目标的同一事实只保留一行，
    行数不随任务次数线性增长。冲突时取高置信度 + 刷新 last_seen + 追加 source_tasks。
    """

    __tablename__ = "recon_facts_agg"
    __table_args__ = (
        UniqueConstraint(
            "target_id", "category", "key", name="uq_recon_facts_agg_tgt_cat_key"
        ),
        Index("ix_recon_facts_agg_tenant", "tenant_id"),
        Index("ix_recon_facts_agg_tgt_conf", "target_id", "confidence"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    target_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("targets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    tenant_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    key: Mapped[str] = mapped_column(String(512), nullable=False)
    value: Mapped[str] = mapped_column(Text, nullable=False, default="")
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.5)
    # 来源任务列表（跨任务收敛累计），JSON 数组。
    source_tasks: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    sensitivity: Mapped[str] = mapped_column(String(16), nullable=False, default="internal")
    details_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    last_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    target = relationship("Target")
    evidence_links = relationship(
        "ReconThreatEvidenceLink",
        back_populates="fact",
        cascade="all, delete-orphan",
    )


class ReconEndpointAgg(Base):
    """recon_endpoints_agg：按 (target_id, host, path_normalized, method) 聚合的资产/端点表。

    对应本地 recon_endpoints，per-target 跨任务收敛。冲突收敛策略同 ReconFactAgg，
    但内容字段（status_code/auth_required/tech_stack 等）为时效数据，采用"最新 wins"，
    仅在 confidence 更高时才覆盖 confidence 本身。
    """

    __tablename__ = "recon_endpoints_agg"
    __table_args__ = (
        UniqueConstraint(
            "target_id",
            "host",
            "path_normalized",
            "method",
            name="uq_recon_endpoints_agg_tgt_host_path_method",
        ),
        Index("ix_recon_endpoints_agg_tenant", "tenant_id"),
        Index("ix_recon_endpoints_agg_tgt_host", "target_id", "host"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    target_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("targets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    tenant_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    host: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    path_normalized: Mapped[str] = mapped_column(String(512), nullable=False)
    method: Mapped[str] = mapped_column(String(16), nullable=False, default="GET")
    status_code: Mapped[int] = mapped_column(Integer, nullable=True)
    auth_required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    tech_stack: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    parameters: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    notes: Mapped[str] = mapped_column(Text, nullable=False, default="")
    discovered_via: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.7)
    # 来源任务列表（跨任务收敛累计），JSON 数组。
    source_tasks: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    last_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    target = relationship("Target")


class ReconThreatAgg(Base):
    """recon_threats_agg：按 (target_id, target_endpoint, category) 聚合的威胁表。

    CVE 为空时以 (target_id, target_endpoint, category) 作为唯一聚合键；
    CVE 非空时追加 cve_id 区分。冲突收敛策略同 ReconFactAgg。
    """

    __tablename__ = "recon_threats_agg"
    __table_args__ = (
        UniqueConstraint(
            "target_id",
            "target_endpoint",
            "category",
            "cve_id",
            name="uq_recon_threats_agg_tgt_ep_cat_cve",
        ),
        Index("ix_recon_threats_agg_tenant", "tenant_id"),
        Index("ix_recon_threats_agg_tgt_status", "target_id", "status"),
        Index("ix_recon_threats_agg_tgt_sev", "target_id", "severity"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    target_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("targets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    tenant_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    cve_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    title: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    category: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    severity: Mapped[str] = mapped_column(String(16), nullable=False, default="medium")
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=ReconAggThreatStatus.suspected.value
    )
    cvss_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    target_endpoint: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    evidence_summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.5)
    source_tasks: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    last_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    target = relationship("Target")
    evidence_links = relationship(
        "ReconThreatEvidenceLink",
        back_populates="threat",
        cascade="all, delete-orphan",
    )


class ReconThreatEvidenceLink(Base):
    """recon_threat_evidence_link：threat ↔ evidence(fact) 多对多关联（v2.1 修订点 #2）。

    一条威胁可关联多条证据事实（如多个受影响端点/参数），一条事实可服务于多个威胁。
    """

    __tablename__ = "recon_threat_evidence_link"
    __table_args__ = (
        UniqueConstraint(
            "threat_agg_id",
            "fact_agg_id",
            name="uq_recon_evidence_link_threat_fact",
        ),
        Index("ix_recon_evidence_link_fact", "fact_agg_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    threat_agg_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("recon_threats_agg.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    fact_agg_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("recon_facts_agg.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    relation: Mapped[str] = mapped_column(String(32), nullable=False, default="supports")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    threat = relationship("ReconThreatAgg", back_populates="evidence_links")
    fact = relationship("ReconFactAgg", back_populates="evidence_links")


# ----------------------------------------------------------------------
# 本地文件沉淀层聚合表（v2.2，设计文档 §本地文件沉淀层落库到 PG）
# ----------------------------------------------------------------------
# 与 recon 聚合层平行的"任务经验沉淀层"：把 per-task 本地文件（Agent 记忆摘要、
# 运行上下文、metrics、rag 索引元数据）以 target_id 为维度聚合进 PG，供后续同目标
# 新任务启动期 seed 复用。认证类文件（agent/auth_context/*）不落库、每次重认证。
# 三表均 sensitivity='internal' 明文、按 target_id+tenant_id 租户隔离、级联挂
# targets/tenants，UNIQUE 约束保证跨任务 upsert 收敛（同一目标同键仅一行）。


class TaskMemoryAgg(Base):
    """task_memory_agg：按 (target_id, agent_role) 聚合的 Agent 经验摘要。

    来源：tasks/<task_id>/agent/<agent_id>/<session_id>/memory/summaries/<role>.md
    """

    __tablename__ = "task_memory_agg"
    __table_args__ = (
        UniqueConstraint(
            "target_id", "agent_role", name="uq_task_memory_agg_tgt_role"
        ),
        Index("ix_task_memory_agg_tenant", "tenant_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    target_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("targets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    tenant_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    agent_role: Mapped[str] = mapped_column(String(64), nullable=False)
    summary_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    source_tasks: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    sensitivity: Mapped[str] = mapped_column(String(16), nullable=False, default="internal")
    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    last_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    target = relationship("Target")


class TaskContextAgg(Base):
    """task_context_agg：按 target_id 聚合的运行上下文（context.txt 全文）。

    来源：tasks/<task_id>/agent/run_context/context.txt
    """

    __tablename__ = "task_context_agg"
    __table_args__ = (
        UniqueConstraint("target_id", name="uq_task_context_agg_tgt"),
        Index("ix_task_context_agg_tenant", "tenant_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    target_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("targets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    tenant_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    content_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    source_tasks: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    sensitivity: Mapped[str] = mapped_column(String(16), nullable=False, default="internal")
    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    last_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    target = relationship("Target")


class TaskMetricsAgg(Base):
    """task_metrics_agg：按 target_id 聚合的会话指标 + RAG 索引引用。

    来源：tasks/<task_id>/metrics/metrics.json + rag/<agent_id>/<session_id>/<target>.db
    （rag 仅存元数据引用，不存向量二进制）。
    """

    __tablename__ = "task_metrics_agg"
    __table_args__ = (
        UniqueConstraint("target_id", name="uq_task_metrics_agg_tgt"),
        Index("ix_task_metrics_agg_tenant", "tenant_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    target_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("targets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    tenant_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    metrics_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    rag_index_ref: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    source_tasks: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    last_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    target = relationship("Target")


class ReconHttpTransactionAgg(Base):
    """recon_http_transactions_agg：按 (target_id, method, url) 聚合的 HTTP 事务表。

    目标级 sitemap 真源（2026-09-02）：任务完成时把本地 recon_http_transactions
    推送至此（url 维度收敛，同一 url 多次请求只留最新），新任务启动时从本表
    seed 目标已有骨架（covered_block / L1 增量提示，避免重复枚举）。

    响应体沿用本地分层存储策略：full（明文）/ compressed（gzip 存
    ``body_compressed``）/ digest（仅摘要）。``source_tasks`` 记录最近来源任务。
    """

    __tablename__ = "recon_http_transactions_agg"
    __table_args__ = (
        UniqueConstraint(
            "target_id", "tenant_id", "method", "url",
            name="uq_recon_http_tx_agg_tgt_method_url",
        ),
        Index("ix_recon_http_tx_agg_tenant", "tenant_id"),
        Index("ix_recon_http_tx_agg_tgt_path", "target_id", "path_normalized"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    target_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("targets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    tenant_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    host: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    path_normalized: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    url: Mapped[str] = mapped_column(String(1024), nullable=False, default="")
    method: Mapped[str] = mapped_column(String(16), nullable=False, default="GET")
    status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="sitemap:katana")
    response_title: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    content_type: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    response_size: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    response_time_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tech_stack: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    parameters: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    auth_used: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    auth_required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    storage_strategy: Mapped[str] = mapped_column(
        String(16), nullable=False, default="full"
    )
    response_body: Mapped[str] = mapped_column(Text, nullable=False, default="")
    body_compressed: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    source_tasks: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    last_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    target = relationship("Target")
