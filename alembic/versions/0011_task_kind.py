"""tasks 表新增 kind 列（区分正式任务与链路探针）

支撑端到端链路连通性探针：probe 类任务标记 kind='probe'，仅做已授权目标
的连通性验证，不深入利用。

Revision ID: 0011_task_kind
Revises: 0010_api_tokens
Create Date: 2026-08-15
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0011_task_kind"
down_revision = "0010_api_tokens"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tasks",
        sa.Column(
            "kind",
            sa.String(length=32),
            nullable=False,
            server_default="task",
        ),
    )
    op.create_index("ix_tasks_kind", "tasks", ["kind"])


def downgrade() -> None:
    op.drop_index("ix_tasks_kind", table_name="tasks")
    op.drop_column("tasks", "kind")
