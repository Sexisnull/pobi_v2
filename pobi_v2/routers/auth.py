"""M4 鉴权路由：注册 / 登录 / 当前用户 / 租户。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pobi_v2.core.config import settings
from pobi_v2.core.deps import get_current_user, require_scope
from pobi_v2.core.exceptions import AppError, ConflictError, NotFoundError
from pobi_v2.core.security import create_access_token, hash_password, verify_password
from pobi_v2.db.models import Tenant, User
from pobi_v2.db.session import get_session
from pobi_v2.schemas.auth import (
    TenantCreate,
    TenantRead,
    TokenResponse,
    UserLogin,
    UserRead,
    UserRegister,
    to_tenant_read,
    to_user_read,
)

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


def _slugify(text: str) -> str:
    return "".join(c if c.isalnum() else "-" for c in text.lower()).strip("-")


# @router.post("/register", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
# async def register(body: UserRegister, session: AsyncSession = Depends(get_session)) -> dict:
#     if not settings.allow_open_registration:
#         raise AppError("当前已关闭开放注册", status_code=status.HTTP_403_FORBIDDEN)
#
#     tenant = (
#         await session.execute(select(Tenant).where(Tenant.slug == body.tenant_slug))
#     ).scalar_one_or_none()
#     if tenant is None:
#         tenant = Tenant(name=body.tenant_slug, slug=body.tenant_slug)
#         session.add(tenant)
#         await session.flush()
#
#     exists = (
#         await session.execute(select(User).where(User.email == body.email))
#     ).scalar_one_or_none()
#     if exists is not None:
#         raise ConflictError("该邮箱已注册")
#
#     user = User(
#         tenant_id=tenant.id,
#         email=body.email,
#         full_name=body.full_name,
#         hashed_password=hash_password(body.password),
#         is_active=True,
#         is_admin=False,
#     )
#     session.add(user)
#     await session.commit()
#     await session.refresh(user)
#     token = create_access_token(str(user.id), str(user.tenant_id))
#     return {
#         "access_token": token,
#         "token_type": "bearer",
#         "user": to_user_read(user),
#     }


@router.post("/login", response_model=TokenResponse)
async def login(body: UserLogin, session: AsyncSession = Depends(get_session)) -> dict:
    user = (
        await session.execute(select(User).where(User.email == body.email))
    ).scalar_one_or_none()
    if user is None or not verify_password(body.password, user.hashed_password):
        raise AppError("邮箱或密码错误", status_code=status.HTTP_401_UNAUTHORIZED)
    if not user.is_active:
        raise AppError("用户已停用", status_code=status.HTTP_403_FORBIDDEN)
    token = create_access_token(str(user.id), str(user.tenant_id))
    return {
        "access_token": token,
        "token_type": "bearer",
        "user": to_user_read(user),
    }


@router.get("/me", response_model=UserRead)
async def me(user: User = Depends(get_current_user)) -> User:
    return user


@router.post("/tenants", response_model=TenantRead, status_code=status.HTTP_201_CREATED)
async def create_tenant(
    body: TenantCreate,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_scope("tenants:write")),
) -> Tenant:
    slug = body.slug or _slugify(body.name)
    exists = (
        await session.execute(select(Tenant).where(Tenant.slug == slug))
    ).scalar_one_or_none()
    if exists is not None:
        raise ConflictError("租户 slug 已存在")
    tenant = Tenant(name=body.name, slug=slug)
    session.add(tenant)
    await session.commit()
    await session.refresh(tenant)
    return tenant
