"""recon 聚合层：新增 recon_http_transactions_agg（per-target HTTP 事务聚合表）

对应本地 recon_http_transactions，把 HTTP 请求/响应事务（含分层存储的响应体）
按 target_id + method + url 维度聚合进 PG，作为目标级 sitemap 真源：
任务完成推送（url 维度收敛，同一 url 只留最新），新任务启动 seed 已有骨架
（covered_block / L1 增量提示）。可回滚。

Revision ID: 0020_recon_http_tx_agg
Revises: 0019_drop_auth_secret
Create Date: 2026-09-02
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0020_recon_http_tx_agg"
down_revision = "0019_drop_auth_secret"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "recon_http_transactions_agg",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("target_id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("host", sa.String(length=255), nullable=False),
        sa.Column("path_normalized", sa.String(length=512), nullable=False),
        sa.Column("url", sa.String(length=1024), nullable=False),
        sa.Column("method", sa.String(length=16), nullable=False),
        sa.Column("status_code", sa.Integer(), nullable=True),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("response_title", sa.String(length=512), nullable=False),
        sa.Column("content_type", sa.String(length=128), nullable=False),
        sa.Column("response_size", sa.Integer(), nullable=False),
        sa.Column("response_time_ms", sa.Integer(), nullable=True),
        sa.Column("tech_stack", sa.JSON(), nullable=False),
        sa.Column("parameters", sa.JSON(), nullable=False),
        sa.Column("auth_used", sa.Boolean(), nullable=False),
        sa.Column("auth_required", sa.Boolean(), nullable=False),
        sa.Column("storage_strategy", sa.String(length=16), nullable=False),
        sa.Column("response_body", sa.Text(), nullable=False),
        sa.Column("body_compressed", sa.LargeBinary(), nullable=True),
        sa.Column("source_tasks", sa.JSON(), nullable=False),
        sa.Column("first_seen", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["target_id"], ["targets.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "target_id",
            "tenant_id",
            "method",
            "url",
            name="uq_recon_http_tx_agg_tgt_method_url",
        ),
    )
    op.create_index(
        "ix_recon_http_tx_agg_tenant", "recon_http_transactions_agg", ["tenant_id"]
    )
    op.create_index(
        "ix_recon_http_tx_agg_tgt_path",
        "recon_http_transactions_agg",
        ["target_id", "path_normalized"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_recon_http_tx_agg_tgt_path", table_name="recon_http_transactions_agg"
    )
    op.drop_index(
        "ix_recon_http_tx_agg_tenant", table_name="recon_http_transactions_agg"
    )
    op.drop_table("recon_http_transactions_agg")
