"""M7 复刻：tasks.confidence 列

Revision ID: 0005_m7_task_confidence
Revises: 0004_m5_approval
Create Date: 2026-08-10
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0005_m7_task_confidence"
down_revision = "0004_m5_approval"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tasks",
        sa.Column("confidence", sa.Float(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("tasks", "confidence")
