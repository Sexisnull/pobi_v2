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
from pobi_v2.core.security import (
    decode_access_token,
    hash_api_token,
)
from pobi_v2.db.models import ApiToken, Tenant, User
from pobi_v2.db.session import get_session

_bearer = HTTPBearer(auto_error=False)


class AuthError(AppError):
    status_code = status.HTTP_401_UNAUTHORIZED
    code = "unauthorized"


def _is_pat(token: str) -> bool:
    """粗略识别个人访问令牌：以 pk_ 前缀，三段式。"""
    return token.startswith("pk_") and token.count("_") >= 2


async def _resolve_pat(token: str, session: AsyncSession) -> User:
    """按 PAT 明文查表校验，返回所属 User（写入 last_used_at）。"""
    token_hash = hash_api_token(token)
    stmt = select(ApiToken).where(ApiToken.token_hash == token_hash)
    result = await session.execute(stmt)
    api_token = result.scalars().first()
    if api_token is None:
        raise AuthError("无效令牌")
    if api_token.revoked:
        raise AuthError("令牌已吊销")
    if api_token.expires_at and api_token.expires_at < _utcnow():
        raise AuthError("令牌已过期")
    user = await session.get(User, api_token.user_id)
    if user is None:
        raise AuthError("用户不存在")
    if not user.is_active:
        raise AuthError("用户已停用")
    # 记录最近使用时间（best-effort，不强制提交失败阻断鉴权）
    api_token.last_used_at = _utcnow()
    try:
        await session.commit()
    except Exception:
        await session.rollback()
    await session.refresh(user, attribute_names=["tenant"])
    # 透传令牌 scopes，供 require_scope 强制执行权限边界
    setattr(user, _EFF_SCOPES_ATTR, list(api_token.scopes or []))
    return user


def _utcnow() -> "datetime":
    from datetime import datetime, timezone

    return datetime.now(timezone.utc)


async def _resolve_user(token: str | None, session: AsyncSession) -> User:
    """解析 Bearer 令牌：优先按 JWT 校验，失败再按 PAT 校验。

    支持登录 JWT 与个人访问令牌（PAT）两种凭证，便于脚本直接调用 API。
    """
    if not token:
        raise AuthError("缺少认证令牌")
    # 先尝试 JWT
    try:
        payload = decode_access_token(token)
        sub = payload.get("sub")
        if not sub:
            raise AuthError("令牌缺少 subject")
        user = await session.get(User, sub)
        if user is None:
            raise AuthError("用户不存在")
        if not user.is_active:
            raise AuthError("用户已停用")
        await session.refresh(user, attribute_names=["tenant"])
        # 登录 JWT 视为全权限
        setattr(user, _EFF_SCOPES_ATTR, ["*"])
        return user
    except (ExpiredSignatureError, InvalidTokenError):
        # JWT 校验失败，尝试 PAT
        if _is_pat(token):
            return await _resolve_pat(token, session)
        raise AuthError("无效令牌")


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


def require_scope_from_query(*required: str):
    """require_scope 的 query 令牌版本，供 SSE 等无法携带 Authorization 头的端点使用。"""

    async def _check(user: User = Depends(get_current_user_from_query)) -> User:
        return await _scope_check(user, required)

    return _check


async def _scope_check(user: User, required: Sequence[str]) -> User:
    eff = _get_effective_scopes(user)
    if "*" in eff:
        return user
    for req in required:
        resource = req.split(":", 1)[0]
        for owned in eff:
            if owned == req or owned == "*":
                return user
            if owned.endswith(":*") and owned.split(":", 1)[0] == resource:
                return user
    owned_repr = ", ".join(eff) if eff else "（空）"
    raise ScopeError(
        f"令牌权限不足：需要 scope {', '.join(required)}，当前持有 {owned_repr}"
    )


def get_tenant_id(user: User = Depends(get_current_user)) -> object:
    return user.tenant_id


# ---------------------------------------------------------------------------
# scope 鉴权：强制执行 API 令牌的 scopes 权限边界（修复越权漏洞）
# ---------------------------------------------------------------------------
# PAT 解析后，将令牌 scopes 附着到 User 实例（运行时属性），供 require_scope 读取。
# 登录 JWT 视为全权限；PAT 按其实体 scopes 字段校验。scope 约定：{resource}:{action}，
# ``*`` 表示全量权限。
_EFF_SCOPES_ATTR = "_effective_scopes"


class ScopeError(AppError):
    """令牌缺少访问该资源所需的 scope。"""

    status_code = status.HTTP_403_FORBIDDEN
    code = "forbidden"


def _get_effective_scopes(user: User) -> list[str]:
    """读取依附在 User 上的有效 scopes；缺失时默认空（最小权限）。"""
    return list(getattr(user, _EFF_SCOPES_ATTR, []) or [])


async def get_effective_scopes(
    user: User = Depends(get_current_user),
) -> list[str]:
    """依赖：返回当前凭证的有效 scope 列表（供路由内部判断或调试）。"""
    return _get_effective_scopes(user)


def require_scope(*required: str):
    """scope 鉴权依赖工厂。

    传入该接口所需的最小 scope（如 ``require_scope("targets:write")``）。
    满足以下任一即放行：有效 scopes 含 ``*``、或与任一 required 精确匹配、
    或持有该资源前缀通配（如 ``targets:*`` 可访问 ``targets:read``）。
    否则抛 403。路由签名中用法::

        user: User = Depends(require_scope("targets:write"))
    """

    async def _check(user: User = Depends(get_current_user)) -> User:
        return await _scope_check(user, required)

    return _check
