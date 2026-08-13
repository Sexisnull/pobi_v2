"""G2/C2 对齐：tasks.agent_mode 列（yolo / hacker 自主策略）

注意：原设计列名 mode 与 PostgreSQL 有序集合聚合函数 mode() 冲突，
在带 ORDER BY 的查询中会触发 "WITHIN GROUP is required" 错误，故重命名为 agent_mode。

Revision ID: 0006_task_mode
Revises: 0005_m7_task_confidence
Create Date: 2026-08-12
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0006_task_mode"
down_revision = "0005_m7_task_confidence"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tasks",
        sa.Column("agent_mode", sa.String(32), nullable=False, server_default="hacker"),
    )


def downgrade() -> None:
    op.drop_column("tasks", "agent_mode")
