"""M5 审批护栏：approval_requests 表

Revision ID: 0004_m5_approval
Revises: 0003_m4_auth
Create Date: 2026-08-10
"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "0004_m5_approval"
down_revision = "0003_m4_auth"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "approval_requests",
        sa.Column("id", UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("task_id", UUID(as_uuid=True),
                  sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("tenant_id", UUID(as_uuid=True),
                  sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("tool_name", sa.String(128), nullable=False),
        sa.Column("agent_name", sa.String(128), nullable=True),
        sa.Column("tool_args", sa.JSON, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("status", sa.Enum("pending", "approved", "rejected", "expired",
                                    name="approvalstatus"), nullable=False,
                  server_default="pending"),
        sa.Column("decision_reason", sa.Text, nullable=True),
        sa.Column("decided_by", UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    )
    op.create_index("ix_approval_requests_task_id", "approval_requests", ["task_id"])
    op.create_index("ix_approval_requests_tenant_id", "approval_requests", ["tenant_id"])
    op.create_index("ix_approval_requests_status", "approval_requests", ["status"])


def downgrade() -> None:
    op.drop_table("approval_requests")
    sa.Enum(name="approvalstatus").drop(op.get_bind(), checkfirst=True)
