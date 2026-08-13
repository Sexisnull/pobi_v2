"""Task 新增 token 用量三列（发送 / 接收 / 总计）

由 scan_workflow 在任务结束时将 CoreAgent.run 的累计 usage 落库，
支撑前端 Token 用量页的展示与成本估算。

Revision ID: 0008_task_tokens
Revises: 0007_target_validation
Create Date: 2026-08-13
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0008_task_tokens"
down_revision = "0007_target_validation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tasks",
        sa.Column(
            "prompt_tokens",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.add_column(
        "tasks",
        sa.Column(
            "completion_tokens",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.add_column(
        "tasks",
        sa.Column(
            "total_tokens",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )


def downgrade() -> None:
    op.drop_column("tasks", "total_tokens")
    op.drop_column("tasks", "completion_tokens")
    op.drop_column("tasks", "prompt_tokens")
