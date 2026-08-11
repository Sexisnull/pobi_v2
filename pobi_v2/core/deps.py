"""M4 鉴权依赖：从请求解析当前用户与租户。

提供：
- get_current_user：校验 Bearer JWT，返回 User ORM（带 tenant 已加载）。
- require_tenant：返回当前用户所属租户 id（资源隔离用）。
"""
from __future__ import annotations

from fastapi import Depends, HTTPException, status
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


async def get_current_user(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
    session: AsyncSession = Depends(get_session),
) -> User:
    """解析 Bearer JWT 并返回已加载 tenant 的 User。"""
    if creds is None or not creds.credentials:
        raise AuthError("缺少认证令牌")
    try:
        payload = decode_access_token(creds.credentials)
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


def get_tenant_id(user: User = Depends(get_current_user)) -> object:
    return user.tenant_id
