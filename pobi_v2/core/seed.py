"""启动时种子数据：当库内无任何用户时，自动创建 admin 账号与默认租户。"""
from __future__ import annotations

import logging

from sqlalchemy import select

from pobi_v2.core.config import settings
from pobi_v2.core.security import hash_password
from pobi_v2.db.models import Tenant, User
from pobi_v2.db.session import AsyncSessionLocal

logger = logging.getLogger("pobi.seed")


async def seed_admin_if_needed() -> None:
    """若库中没有用户，则创建 admin 账号及其默认租户。

    幂等：仅在 users 表为空时执行，避免重复创建。
    """
    async with AsyncSessionLocal() as session:
        # 仅当库内不存在 admin 用户时才创建，避免重复；普通用户存在不影响
        existing_admin = (
            await session.execute(select(User).where(User.is_admin.is_(True)).limit(1))
        ).scalar_one_or_none()
        if existing_admin is not None:
            logger.info("[seed] 已存在 admin 账号，跳过创建")
            return

        tenant = Tenant(name=settings.admin_tenant_slug, slug=settings.admin_tenant_slug)
        session.add(tenant)
        await session.flush()

        admin = User(
            tenant_id=tenant.id,
            email=settings.admin_email,
            full_name=settings.admin_full_name,
            hashed_password=hash_password(settings.admin_password),
            is_active=True,
            is_admin=True,
        )
        session.add(admin)
        await session.commit()
        logger.info(
            "[seed] 已创建 admin 账号：%s / %s",
            settings.admin_email,
            settings.admin_password,
        )
