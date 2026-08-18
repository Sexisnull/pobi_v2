"""tasks 表新增任务级验证策略字段

验证策略配置从「授权目标级」下沉到「任务级」：每个任务可独立配置
怎样才算找到漏洞（flag 正则、验证格式、信心阈值带、任务树深度）。
字段可空，None 表示继承授权目标配置或使用默认。

Revision ID: 0012_task_validation
Revises: 0011_task_kind
Create Date: 2026-08-18
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0012_task_validation"
down_revision = "0011_task_kind"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tasks",
        sa.Column("flag_regex", sa.String(512), nullable=True),
    )
    op.add_column(
        "tasks",
        sa.Column("validation_format", sa.String(64), nullable=True),
    )
    op.add_column(
        "tasks",
        sa.Column("confidence_threshold", sa.Float(), nullable=True),
    )
    op.add_column(
        "tasks",
        sa.Column("max_tree_depth", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("tasks", "max_tree_depth")
    op.drop_column("tasks", "confidence_threshold")
    op.drop_column("tasks", "validation_format")
    op.drop_column("tasks", "flag_regex")
