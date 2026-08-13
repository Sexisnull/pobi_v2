"""G2/C2 对齐：targets 增加验证策略（Validation Configuration）字段

授权目标配置补充「怎样才算找到漏洞」的验证策略，对齐 deadend-cli 的
Validation Configuration：flag 正则、验证格式、信心阈值带、任务树深度。
运行任务时这些字段写入全局 validation.yaml，复用原 ValidationGate(Flag+Judge)。

Revision ID: 0007_target_validation
Revises: 0006_task_mode
Create Date: 2026-08-12
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0007_target_validation"
down_revision = "0006_task_mode"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "targets",
        sa.Column("flag_regex", sa.String(512), nullable=True),
    )
    op.add_column(
        "targets",
        sa.Column("validation_format", sa.String(64), nullable=True),
    )
    op.add_column(
        "targets",
        sa.Column(
            "confidence_threshold",
            sa.Float(),
            nullable=False,
            server_default=sa.text("0.6"),
        ),
    )
    op.add_column(
        "targets",
        sa.Column(
            "max_tree_depth",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("4"),
        ),
    )


def downgrade() -> None:
    op.drop_column("targets", "max_tree_depth")
    op.drop_column("targets", "confidence_threshold")
    op.drop_column("targets", "validation_format")
    op.drop_column("targets", "flag_regex")
