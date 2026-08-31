"""recon 聚合层：新增 recon_endpoints_agg（per-target 资产/端点聚合表）

对应本地 recon_endpoints，把端点/资产清单按 target_id 维度聚合进 PG，
支撑目标全景图的资产视图。可回滚。

Revision ID: 0017_recon_endpoints_agg
Revises: 0016_artifact_agg
Create Date: 2026-08-31
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0017_recon_endpoints_agg"
down_revision = "0016_artifact_agg"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "recon_endpoints_agg",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("target_id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("host", sa.String(length=255), nullable=False),
        sa.Column("path_normalized", sa.String(length=512), nullable=False),
        sa.Column("method", sa.String(length=16), nullable=False),
        sa.Column("status_code", sa.Integer(), nullable=True),
        sa.Column("auth_required", sa.Boolean(), nullable=False),
        sa.Column("tech_stack", sa.JSON(), nullable=False),
        sa.Column("parameters", sa.JSON(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=False),
        sa.Column("discovered_via", sa.String(length=128), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("source_tasks", sa.JSON(), nullable=False),
        sa.Column("first_seen", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["target_id"], ["targets.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "target_id",
            "host",
            "path_normalized",
            "method",
            name="uq_recon_endpoints_agg_tgt_host_path_method",
        ),
    )
    op.create_index(
        "ix_recon_endpoints_agg_tenant", "recon_endpoints_agg", ["tenant_id"]
    )
    op.create_index(
        "ix_recon_endpoints_agg_tgt_host",
        "recon_endpoints_agg",
        ["target_id", "host"],
    )


def downgrade() -> None:
    op.drop_index("ix_recon_endpoints_agg_tgt_host", table_name="recon_endpoints_agg")
    op.drop_index("ix_recon_endpoints_agg_tenant", table_name="recon_endpoints_agg")
    op.drop_table("recon_endpoints_agg")
