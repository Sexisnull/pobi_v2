"""审计外键摘除：解除 audit_events 与业务实体的外键约束，使其成为不可变历史引用

问题：0021 为 audit_events 装载 append-only 触发器（禁止 UPDATE / DELETE），
但该表 ``task_id`` / ``target_id`` / ``actor_id`` / ``tenant_id`` 的外键均为
``ON DELETE SET NULL``。删除任务时 PostgreSQL 需把这些行的外键列置 NULL，
这是一次 UPDATE，直接撞上 append-only 触发器：

    psycopg.errors.RaiseException: audit_events is append-only: UPDATE denied
    [SQL: DELETE FROM tasks WHERE tasks.id = %s::UUID]

整个事务回滚，表现为「删除任务按钮点了没反应（后端 500）」。

修复：摘掉 audit_events 的四个外键约束，外键列保留原值成为**不可变历史引用**。
这正是 0021 已确立的设计意图（``models.py`` 注释：「审计证据须长于实体本身」）——
实体删除后审计行仍完整保留其 task_id / target_id / actor_id，可追溯且不被篡改。
去掉约束后，删除 tasks / targets / users / tenants 均不再触发对 audit_events 的
任何写操作，append-only 语义得以严格成立。

代价：外键列可能指向已删除实体（悬空 UUID 引用）。查询侧不做 join 保证，
前端以 shortId 展示，属预期行为。

Revision ID: 0022_audit_detach_fk
Revises: 0021_audit_governance
Create Date: 2026-09-10
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0022_audit_detach_fk"
down_revision = "0021_audit_governance"
branch_labels = None
depends_on = None

# (约束名, 列名, 被引用表, 列类型)
_FKS: tuple[tuple[str, str, str, sa.types.TypeEngine], ...] = (
    ("audit_events_task_id_fkey", "task_id", "tasks", sa.Uuid()),
    ("audit_events_target_id_fkey", "target_id", "targets", sa.Uuid()),
    ("audit_events_tenant_id_fkey", "tenant_id", "tenants", sa.Uuid()),
    ("audit_events_actor_id_fkey", "actor_id", "users", sa.Uuid()),
)


def upgrade() -> None:
    bind = op.get_bind()
    for name, _col, _ref, _type in _FKS:
        op.drop_constraint(name, "audit_events", type_="foreignkey")
    # 非 PG 方言（sqlite，仅测试）无命名约束：由 metadata 重建时直接不建 FK。
    # 此处仅提示，不做额外处理。
    if bind.dialect.name != "postgresql":
        return


def downgrade() -> None:
    # 回退会重新引入 SET NULL 语义，从而再次与 append-only 触发器冲突。
    # 仅在确实需要恢复外键完整性时使用，且须先禁用触发器。
    for name, col, ref, _type in _FKS:
        op.create_foreign_key(
            name,
            "audit_events",
            ref,
            [col],
            ["id"],
            ondelete="SET NULL",
        )
