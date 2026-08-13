"""M4 鉴权依赖：从请求解析当前用户与租户。

提供：
- get_current_user：校验 Bearer JWT，返回 User ORM（带 tenant 已加载）。
- require_tenant：返回当前用户所属租户 id（资源隔离用）。
"""
from __future__ import annotations

from fastapi import Depends, HTTPException, Query, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt.exceptions import ExpiredSignatureError, InvalidTokenError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pobi_v2.core.exceptions import AppError
from pobi_v2.core.security import decode_access_token
from pobi_v2.db.models import Tenant, User
from pobi_v2.db.session import get_session

_bearer = HTTPBearer(auto_error=False)


class AuthError(AppError):
    status_code = status.HTTP_401_UNAUTHORIZED
    code = "unauthorized"


async def _resolve_user(token: str | None, session: AsyncSession) -> User:
    """按 JWT 字符串解析并返回已加载 tenant 的 User。"""
    if not token:
        raise AuthError("缺少认证令牌")
    try:
        payload = decode_access_token(token)
    except ExpiredSignatureError:
        raise AuthError("令牌已过期")
    except InvalidTokenError:
        raise AuthError("无效令牌")

    sub = payload.get("sub")
    if not sub:
        raise AuthError("令牌缺少 subject")
    user = await session.get(User, sub)
    if user is None:
        raise AuthError("用户不存在")
    if not user.is_active:
        raise AuthError("用户已停用")
    # 确保 tenant 已加载
    await session.refresh(user, attribute_names=["tenant"])
    return user


async def get_current_user(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
    session: AsyncSession = Depends(get_session),
) -> User:
    """解析 Bearer JWT 并返回已加载 tenant 的 User。"""
    token = creds.credentials if creds else None
    return await _resolve_user(token, session)


async def get_current_user_from_query(
    token: str | None = Query(default=None, description="SSE/EventSource 通过 query 传递的 JWT"),
    session: AsyncSession = Depends(get_session),
) -> User:
    """兼容浏览器原生 EventSource：无法设置 Authorization 头，改由 ?token= 传递。"""
    return await _resolve_user(token, session)


def get_tenant_id(user: User = Depends(get_current_user)) -> object:
    return user.tenant_id
