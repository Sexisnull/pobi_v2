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
