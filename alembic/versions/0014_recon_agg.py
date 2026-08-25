"""recon 聚合层：新增 recon_facts_agg / recon_threats_agg / recon_threat_evidence_link

把 per-task 本地 SQLite 侦察资产以 target_id 维度聚合进 PG，作为跨任务记忆层
与续扫基线源（设计文档 §5，第二阶）。可回滚。

Revision ID: 0014_recon_agg
Revises: 0013_task_is_range
Create Date: 2026-08-19
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0014_recon_agg"
down_revision = "0013_task_is_range"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "recon_facts_agg",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("target_id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("category", sa.String(length=32), nullable=False),
        sa.Column("key", sa.String(length=512), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("source_tasks", sa.JSON(), nullable=False),
        sa.Column("sensitivity", sa.String(length=16), nullable=False),
        sa.Column("details_json", sa.JSON(), nullable=False),
        sa.Column("first_seen", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["target_id"], ["targets.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "target_id", "category", "key", name="uq_recon_facts_agg_tgt_cat_key"
        ),
    )
    op.create_index("ix_recon_facts_agg_tenant", "recon_facts_agg", ["tenant_id"])
    op.create_index(
        "ix_recon_facts_agg_tgt_conf", "recon_facts_agg", ["target_id", "confidence"]
    )

    op.create_table(
        "recon_threats_agg",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("target_id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("cve_id", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=512), nullable=False),
        sa.Column("category", sa.String(length=64), nullable=False),
        sa.Column("severity", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("cvss_score", sa.Float(), nullable=True),
        sa.Column("target_endpoint", sa.String(length=512), nullable=False),
        sa.Column("evidence_summary", sa.Text(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("source_tasks", sa.JSON(), nullable=False),
        sa.Column("first_seen", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["target_id"], ["targets.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "target_id",
            "target_endpoint",
            "category",
            "cve_id",
            name="uq_recon_threats_agg_tgt_ep_cat_cve",
        ),
    )
    op.create_index("ix_recon_threats_agg_tenant", "recon_threats_agg", ["tenant_id"])
    op.create_index(
        "ix_recon_threats_agg_tgt_status",
        "recon_threats_agg",
        ["target_id", "status"],
    )
    op.create_index(
        "ix_recon_threats_agg_tgt_sev", "recon_threats_agg", ["target_id", "severity"]
    )

    op.create_table(
        "recon_threat_evidence_link",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("threat_agg_id", sa.Uuid(), nullable=False),
        sa.Column("fact_agg_id", sa.Uuid(), nullable=False),
        sa.Column("relation", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["threat_agg_id"],
            ["recon_threats_agg.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["fact_agg_id"], ["recon_facts_agg.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "threat_agg_id",
            "fact_agg_id",
            name="uq_recon_evidence_link_threat_fact",
        ),
    )
    op.create_index(
        "ix_recon_evidence_link_fact",
        "recon_threat_evidence_link",
        ["fact_agg_id"],
    )


def downgrade() -> None:
    op.drop_table("recon_threat_evidence_link")
    op.drop_table("recon_threats_agg")
    op.drop_table("recon_facts_agg")
