"""新增 pricing_config 表（用户自定义 LLM 单价）

单条价格配置（每百万 token 单价），供 Token 用量页估算成本。

Revision ID: 0009_pricing_config
Revises: 0008_task_tokens
Create Date: 2026-08-13
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0009_pricing_config"
down_revision = "0008_task_tokens"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "pricing_config",
        sa.Column("id", sa.String(32), primary_key=True, default="default"),
        sa.Column(
            "price_input", sa.Float(), nullable=False, server_default=sa.text("0.0")
        ),
        sa.Column(
            "price_output", sa.Float(), nullable=False, server_default=sa.text("0.0")
        ),
        sa.Column(
            "currency", sa.String(8), nullable=False, server_default=sa.text("'USD'")
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_table("pricing_config")
