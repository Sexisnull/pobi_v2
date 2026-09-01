"""task 表新增认证前置（PreAuth）配置字段

任务创建阶段完成登录所需的认证配置与状态：auth_mode / auth_status /
凭据（Fernet 加密）/ 登录地址 / 会话 profile 名。可回滚。

Revision ID: 0018_task_auth
Revises: 0017_recon_endpoints_agg
Create Date: 2026-09-01
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0018_task_auth"
down_revision = "0017_recon_endpoints_agg"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tasks", sa.Column("auth_mode", sa.String(length=16), nullable=False, server_default="none"))
    op.add_column("tasks", sa.Column("auth_status", sa.String(length=24), nullable=False, server_default="none"))
    op.add_column("tasks", sa.Column("auth_username", sa.String(length=255), nullable=True))
    op.add_column("tasks", sa.Column("auth_secret", sa.Text(), nullable=True))
    op.add_column("tasks", sa.Column("auth_login_url", sa.String(length=2048), nullable=True))
    op.add_column("tasks", sa.Column("auth_profile", sa.String(length=64), nullable=False, server_default="preauth"))
    op.add_column("tasks", sa.Column("auth_error", sa.Text(), nullable=True))
    op.add_column("tasks", sa.Column("auth_updated_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("tasks", "auth_updated_at")
    op.drop_column("tasks", "auth_error")
    op.drop_column("tasks", "auth_profile")
    op.drop_column("tasks", "auth_login_url")
    op.drop_column("tasks", "auth_secret")
    op.drop_column("tasks", "auth_username")
    op.drop_column("tasks", "auth_status")
    op.drop_column("tasks", "auth_mode")
