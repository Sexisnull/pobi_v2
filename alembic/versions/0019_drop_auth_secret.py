"""删除 tasks.auth_secret 列：凭据不再落库

安全强化：账号密码属于不可复用资产（登录态会失效），从创建起即不落任何
数据库（pgsql/sqlite），仅写入任务目录钱包
``tasks/<task_id>/reusable_credentials.json`` 供 authenticator 重认证消费。
此迁移删除 0018 新增的 ``auth_secret``（Fernet 加密密码）列，历史已存值
一并移除，不再保留。可回滚（回滚恢复列，但历史数据已不可恢复）。

Revision ID: 0019_drop_auth_secret
Revises: 0018_task_auth
Create Date: 2026-09-02
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0019_drop_auth_secret"
down_revision = "0018_task_auth"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("tasks", "auth_secret")


def downgrade() -> None:
    op.add_column("tasks", sa.Column("auth_secret", sa.Text(), nullable=True))
