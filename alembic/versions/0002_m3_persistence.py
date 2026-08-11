"""M3 persistence: findings / audit / task_events / artifacts + task extensions

Revision ID: 0002_m3_persistence
Revises: 0001_initial
Create Date: 2026-08-10
"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

revision = "0002_m3_persistence"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ---- tasks: 扩展字段（取消 / 续跑 / 审计）----
    op.add_column("tasks", sa.Column("cancel_requested", sa.Boolean, nullable=False,
                                     server_default=sa.false()))
    op.add_column("tasks", sa.Column("attempts", sa.Integer, nullable=False,
                                     server_default=sa.text("0")))
    op.add_column("tasks", sa.Column("last_agent_session", sa.String(128), nullable=True))

    # ---- task_events：有序运行轨迹 ----
    op.create_table(
        "task_events",
        sa.Column("id", UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("task_id", UUID(as_uuid=True),
                  sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("seq", sa.Integer, nullable=False),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("payload", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    )
    op.create_index("ix_task_events_task_id", "task_events", ["task_id"])
    op.create_index("ix_task_events_event_type", "task_events", ["event_type"])

    # ---- findings：渗透发现 ----
    op.create_table(
        "findings",
        sa.Column("id", UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("task_id", UUID(as_uuid=True),
                  sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("target_id", UUID(as_uuid=True),
                  sa.ForeignKey("targets.id", ondelete="CASCADE"), nullable=False),
        sa.Column("title", sa.String(512), nullable=False),
        sa.Column("severity", sa.Enum("info", "low", "medium", "high", "critical",
                                      name="severity"), nullable=False, server_default="info"),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("evidence", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("confidence", sa.Float, nullable=True),
        sa.Column("cwe", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    )
    op.create_index("ix_findings_task_id", "findings", ["task_id"])
    op.create_index("ix_findings_target_id", "findings", ["target_id"])
    op.create_index("ix_findings_severity", "findings", ["severity"])

    # ---- artifacts：产物元数据 ----
    op.create_table(
        "artifacts",
        sa.Column("id", UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("task_id", UUID(as_uuid=True),
                  sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("finding_id", UUID(as_uuid=True),
                  sa.ForeignKey("findings.id", ondelete="CASCADE"), nullable=True),
        sa.Column("kind", sa.Enum("screenshot", "poc", "report", "log", "other",
                                  name="artifactkind"), nullable=False, server_default="other"),
        sa.Column("name", sa.String(512), nullable=False),
        sa.Column("storage_key", sa.String(1024), nullable=True),
        sa.Column("content", sa.Text, nullable=True),
        sa.Column("content_type", sa.String(128), nullable=True),
        sa.Column("size_bytes", sa.Integer, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    )
    op.create_index("ix_artifacts_task_id", "artifacts", ["task_id"])
    op.create_index("ix_artifacts_finding_id", "artifacts", ["finding_id"])
    op.create_index("ix_artifacts_kind", "artifacts", ["kind"])

    # ---- audit_events：结构化审计 ----
    op.create_table(
        "audit_events",
        sa.Column("id", UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("task_id", UUID(as_uuid=True),
                  sa.ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True),
        sa.Column("target_id", UUID(as_uuid=True),
                  sa.ForeignKey("targets.id", ondelete="SET NULL"), nullable=True),
        sa.Column("actor", sa.String(128), nullable=False, server_default="web-operator"),
        sa.Column("action", sa.String(128), nullable=False),
        sa.Column("outcome", sa.String(32), nullable=False, server_default="success"),
        sa.Column("detail", sa.Text, nullable=True),
        sa.Column("meta", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    )
    op.create_index("ix_audit_events_task_id", "audit_events", ["task_id"])
    op.create_index("ix_audit_events_target_id", "audit_events", ["target_id"])
    op.create_index("ix_audit_events_action", "audit_events", ["action"])
    op.create_index("ix_audit_events_created_at", "audit_events", ["created_at"])


def downgrade() -> None:
    op.drop_table("audit_events")
    op.drop_table("artifacts")
    sa.Enum(name="artifactkind").drop(op.get_bind(), checkfirst=True)
    op.drop_table("findings")
    sa.Enum(name="severity").drop(op.get_bind(), checkfirst=True)
    op.drop_table("task_events")
    op.drop_column("tasks", "last_agent_session")
    op.drop_column("tasks", "attempts")
    op.drop_column("tasks", "cancel_requested")
