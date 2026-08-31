"""本地文件沉淀层聚合表：task_memory_agg / task_context_agg / task_metrics_agg

把 per-task 本地文件（Agent 记忆摘要、运行上下文、metrics、rag 索引元数据）
以 target_id 维度聚合进 PG，供后续同目标新任务启动期 seed 复用（设计文档
§本地文件沉淀层落库到 PG）。认证类文件（agent/auth_context/*）不落库、每次重认证。
三表均 sensitivity='internal' 明文、按 target_id+tenant_id 租户隔离、级联挂
targets/tenants。可回滚。

Revision ID: 0016_artifact_agg
Revises: 0015_artifact_target
Create Date: 2026-08-31
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0016_artifact_agg"
down_revision = "0015_artifact_target"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "task_memory_agg",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("target_id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("agent_role", sa.String(length=64), nullable=False),
        sa.Column("summary_text", sa.Text(), nullable=False),
        sa.Column("source_tasks", sa.JSON(), nullable=False),
        sa.Column("sensitivity", sa.String(length=16), nullable=False),
        sa.Column("first_seen", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["target_id"], ["targets.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "target_id", "agent_role", name="uq_task_memory_agg_tgt_role"
        ),
    )
    op.create_index("ix_task_memory_agg_tenant", "task_memory_agg", ["tenant_id"])

    op.create_table(
        "task_context_agg",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("target_id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("content_text", sa.Text(), nullable=False),
        sa.Column("source_tasks", sa.JSON(), nullable=False),
        sa.Column("sensitivity", sa.String(length=16), nullable=False),
        sa.Column("first_seen", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["target_id"], ["targets.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("target_id", name="uq_task_context_agg_tgt"),
    )
    op.create_index("ix_task_context_agg_tenant", "task_context_agg", ["tenant_id"])

    op.create_table(
        "task_metrics_agg",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("target_id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("metrics_json", sa.JSON(), nullable=False),
        sa.Column("rag_index_ref", sa.JSON(), nullable=False),
        sa.Column("source_tasks", sa.JSON(), nullable=False),
        sa.Column("first_seen", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["target_id"], ["targets.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("target_id", name="uq_task_metrics_agg_tgt"),
    )
    op.create_index("ix_task_metrics_agg_tenant", "task_metrics_agg", ["tenant_id"])


def downgrade() -> None:
    op.drop_table("task_metrics_agg")
    op.drop_table("task_context_agg")
    op.drop_table("task_memory_agg")
