"""artifacts 补 target_id：支持按目标聚合产物（目标总览）

报告正文等产物需要按 target_id 维度聚合展示，并具备独立的租户隔离
基础（与 findings 一致，后续可经 target.tenant_id 推导隔离）。

Revision ID: 0015_artifact_target
Revises: 0014_recon_agg
Create Date: 2026-08-27
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0015_artifact_target"
down_revision = "0014_recon_agg"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "artifacts",
        sa.Column(
            "target_id",
            sa.Uuid(),
            sa.ForeignKey("targets.id", ondelete="CASCADE"),
            nullable=True,
        ),
    )
    op.create_index("ix_artifacts_target", "artifacts", ["target_id"])


def downgrade() -> None:
    op.drop_index("ix_artifacts_target", table_name="artifacts")
    op.drop_column("artifacts", "target_id")
