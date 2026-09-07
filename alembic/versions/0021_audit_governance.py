"""审计治理增强：追踪关联、防篡改哈希链、证据留存与 append-only 约束

1. ``audit_events`` / ``task_events`` 增加 ``trace_id`` / ``span_id``，审计可跳转链路
2. ``audit_events`` 增加 ``prev_hash`` / ``hash`` 并回填历史数据形成哈希链
   （算法复用 ``pobi_v2.db.persistence.audit_row_hash``，与在线写入保持一致；
   若该算法后续变更，需新迁移重算全链）
3. ``audit_events.tenant_id`` 外键由 CASCADE 改为 SET NULL：租户删除不得抹除审计证据
4. PostgreSQL 增加 append-only 触发器，禁止 UPDATE / DELETE（运维清理需显式禁用触发器）
   非 PG 方言（sqlite，仅测试）跳过 3、4，由 metadata 建表时直接应用 SET NULL 语义

Revision ID: 0021_audit_governance
Revises: 0020_recon_http_tx_agg
Create Date: 2026-09-07
"""
from __future__ import annotations

import json

import sqlalchemy as sa
from alembic import op

from pobi_v2.db.persistence import audit_row_hash

revision = "0021_audit_governance"
down_revision = "0020_recon_http_tx_agg"
branch_labels = None
depends_on = None

_AUDIT_SELECT = sa.text(
    "SELECT id, task_id, target_id, tenant_id, actor_id, actor, action, outcome,"
    " detail, meta, created_at FROM audit_events ORDER BY created_at, id"
)
_AUDIT_UPDATE = sa.text(
    "UPDATE audit_events SET prev_hash = :prev_hash, hash = :hash WHERE id = :id"
)

_APPEND_ONLY_FN = """
CREATE OR REPLACE FUNCTION audit_events_append_only() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'audit_events is append-only: % denied', TG_OP;
END;
$$ LANGUAGE plpgsql;
"""


def _as_dict(value) -> dict:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    try:
        loaded = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _backfill_hash_chain() -> None:
    """按 created_at, id 顺序为历史审计行补齐哈希链。"""
    bind = op.get_bind()
    prev: str | None = None
    for row in bind.execute(_AUDIT_SELECT):
        payload = {
            "id": str(row.id),
            "task_id": str(row.task_id) if row.task_id else None,
            "target_id": str(row.target_id) if row.target_id else None,
            "tenant_id": str(row.tenant_id) if row.tenant_id else None,
            "actor_id": str(row.actor_id) if row.actor_id else None,
            "actor": row.actor,
            "action": row.action,
            "outcome": row.outcome,
            "detail": row.detail,
            "meta": _as_dict(row.meta),
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }
        row_hash = audit_row_hash(payload, prev)
        bind.execute(_AUDIT_UPDATE, {"prev_hash": prev, "hash": row_hash, "id": row.id})
        prev = row_hash


def upgrade() -> None:
    op.add_column("audit_events", sa.Column("trace_id", sa.String(32), nullable=True))
    op.add_column("audit_events", sa.Column("span_id", sa.String(16), nullable=True))
    op.add_column("audit_events", sa.Column("prev_hash", sa.String(64), nullable=True))
    op.add_column("audit_events", sa.Column("hash", sa.String(64), nullable=True))
    op.create_index("ix_audit_events_trace_id", "audit_events", ["trace_id"])

    op.add_column("task_events", sa.Column("trace_id", sa.String(32), nullable=True))
    op.add_column("task_events", sa.Column("span_id", sa.String(16), nullable=True))
    op.create_index("ix_task_events_trace_id", "task_events", ["trace_id"])

    if op.get_bind().dialect.name == "postgresql":
        op.drop_constraint("audit_events_tenant_id_fkey", "audit_events", type_="foreignkey")
        op.create_foreign_key(
            "audit_events_tenant_id_fkey",
            "audit_events",
            "tenants",
            ["tenant_id"],
            ["id"],
            ondelete="SET NULL",
        )
        op.execute(_APPEND_ONLY_FN)
        op.execute(
            "CREATE TRIGGER audit_events_no_update BEFORE UPDATE ON audit_events"
            " FOR EACH ROW EXECUTE FUNCTION audit_events_append_only()"
        )
        op.execute(
            "CREATE TRIGGER audit_events_no_delete BEFORE DELETE ON audit_events"
            " FOR EACH ROW EXECUTE FUNCTION audit_events_append_only()"
        )

    _backfill_hash_chain()


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP TRIGGER IF EXISTS audit_events_no_delete ON audit_events")
        op.execute("DROP TRIGGER IF EXISTS audit_events_no_update ON audit_events")
        op.execute("DROP FUNCTION IF EXISTS audit_events_append_only()")
        op.drop_constraint("audit_events_tenant_id_fkey", "audit_events", type_="foreignkey")
        op.create_foreign_key(
            "audit_events_tenant_id_fkey",
            "audit_events",
            "tenants",
            ["tenant_id"],
            ["id"],
            ondelete="CASCADE",
        )

    op.drop_index("ix_task_events_trace_id", table_name="task_events")
    op.drop_column("task_events", "span_id")
    op.drop_column("task_events", "trace_id")

    op.drop_index("ix_audit_events_trace_id", table_name="audit_events")
    op.drop_column("audit_events", "hash")
    op.drop_column("audit_events", "prev_hash")
    op.drop_column("audit_events", "span_id")
    op.drop_column("audit_events", "trace_id")
