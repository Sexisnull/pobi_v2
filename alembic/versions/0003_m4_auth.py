"""M4 多租户鉴权：tenants / users + 资源归属列

Revision ID: 0003_m4_auth
Revises: 0002_m3_persistence
Create Date: 2026-08-10
"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "0003_m4_auth"
down_revision = "0002_m3_persistence"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ---- tenants ----
    op.create_table(
        "tenants",
        sa.Column("id", UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("name", sa.String(255), nullable=False, unique=True),
        sa.Column("slug", sa.String(128), nullable=False, unique=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    )

    # ---- users ----
    op.create_table(
        "users",
        sa.Column("id", UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", UUID(as_uuid=True),
                  sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("email", sa.String(320), nullable=False, unique=True),
        sa.Column("full_name", sa.String(255), nullable=True),
        sa.Column("hashed_password", sa.String(255), nullable=False),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("is_admin", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    )
    op.create_index("ix_users_tenant_id", "users", ["tenant_id"])

    # ---- targets：加 tenant_id / owner_id ----
    op.add_column("targets", sa.Column("tenant_id", UUID(as_uuid=True),
                 sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False))
    op.add_column("targets", sa.Column("owner_id", UUID(as_uuid=True),
                 sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True))
    # URL 唯一约束在租户维度放开（不同租户可同 URL）
    # 历史约束可能以任意名称存在，使用 DO block 安全删除（幂等）
    op.execute(
        sa.text(
            """
            DO $$
            BEGIN
              IF EXISTS (
                SELECT 1 FROM pg_constraint c
                JOIN pg_class t ON t.oid = c.conrelid
                JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = ANY(c.conkey)
                WHERE t.relname = 'targets' AND a.attname = 'url'
                  AND c.contype = 'u'
              ) THEN
                EXECUTE (
                  SELECT format('ALTER TABLE targets DROP CONSTRAINT %I',
                    (SELECT conname FROM pg_constraint c
                     JOIN pg_class t ON t.oid = c.conrelid
                     JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = ANY(c.conkey)
                     WHERE t.relname = 'targets' AND a.attname = 'url'
                       AND c.contype = 'u' LIMIT 1))
                );
              END IF;
            END $$;
            """
        )
    )
    op.create_index("ix_targets_tenant_id", "targets", ["tenant_id"])
    op.create_index("ix_targets_owner_id", "targets", ["owner_id"])

    # ---- tasks：加 tenant_id / owner_id ----
    op.add_column("tasks", sa.Column("tenant_id", UUID(as_uuid=True),
                 sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False))
    op.add_column("tasks", sa.Column("owner_id", UUID(as_uuid=True),
                 sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True))
    op.create_index("ix_tasks_tenant_id", "tasks", ["tenant_id"])
    op.create_index("ix_tasks_owner_id", "tasks", ["owner_id"])

    # ---- audit_events：加 tenant_id / actor_id ----
    op.add_column("audit_events", sa.Column("tenant_id", UUID(as_uuid=True),
                 sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=True))
    op.add_column("audit_events", sa.Column("actor_id", UUID(as_uuid=True),
                 sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True))
    op.create_index("ix_audit_events_tenant_id", "audit_events", ["tenant_id"])
    op.create_index("ix_audit_events_actor_id", "audit_events", ["actor_id"])


def downgrade() -> None:
    op.drop_index("ix_audit_events_actor_id", "audit_events")
    op.drop_index("ix_audit_events_tenant_id", "audit_events")
    op.drop_column("audit_events", "actor_id")
    op.drop_column("audit_events", "tenant_id")

    op.drop_index("ix_tasks_owner_id", "tasks")
    op.drop_index("ix_tasks_tenant_id", "tasks")
    op.drop_column("tasks", "owner_id")
    op.drop_column("tasks", "tenant_id")

    op.drop_index("ix_targets_owner_id", "targets")
    op.drop_index("ix_targets_tenant_id", "targets")
    op.drop_column("targets", "owner_id")
    op.drop_column("targets", "tenant_id")
    op.create_unique_constraint("uq_targets_url", "targets", ["url"])

    op.drop_table("users")
    op.drop_table("tenants")
