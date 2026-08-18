"""tasks 表新增 is_range（是否靶场）字段

验证策略判定从「flag 正则是否非空」隐式推断，改为由用户显式意图驱动：
- is_range=True：靶场 / 夺旗任务，必须配置 flag_regex 供验证 Agent 验收
- is_range=False（默认）：真实目标，走 judge-only（objective + LLM 自行判断完成）

Revision ID: 0013_task_is_range
Revises: 0012_task_validation
Create Date: 2026-08-18
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0013_task_is_range"
down_revision = "0012_task_validation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tasks",
        sa.Column("is_range", sa.Boolean(), server_default=sa.false(), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("tasks", "is_range")
