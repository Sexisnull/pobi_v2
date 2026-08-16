"""API 令牌（PAT）管理路由。

用于脚本 / 第三方直接调用项目 API 的长期令牌：
- 创建（返回明文，可点击查看前提是配置了加密密钥）
- 列表（不含明文）
- 吊销
- 点击查看明文（reveal）

鉴权：本路由自身使用登录 JWT（get_current_user），仅管理当前用户自己的令牌。
生成的 PAT 用于调用项目其他 API（tasks / targets 等），其校验在 core.deps 的
get_current_user 中统一支持。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pobi_v2.core.deps import get_current_user, require_scope
from pobi_v2.core.security import (
    decrypt_api_token,
    encrypt_api_token,
    generate_api_token,
)
from pobi_v2.db.models import ApiToken, User
from pobi_v2.db.session import get_session
from pobi_v2.schemas.token import (
    ApiTokenCreate,
    ApiTokenCreated,
    ApiTokenRead,
    ApiTokenReveal,
)

router = APIRouter(prefix="/api/v1/tokens", tags=["api-tokens"])


@router.post("", response_model=ApiTokenCreated, status_code=status.HTTP_201_CREATED)
async def create_token(
    data: ApiTokenCreate,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_scope("tokens:write")),
) -> ApiTokenCreated:
    plaintext, prefix, token_hash = generate_api_token()
    expires_at = None
    if data.expires_in_days:
        expires_at = datetime.now(timezone.utc) + timedelta(days=data.expires_in_days)

    token = ApiToken(
        user_id=user.id,
        tenant_id=user.tenant_id,
        name=data.name,
        prefix=prefix,
        token_hash=token_hash,
        encrypted_secret=encrypt_api_token(plaintext),
        scopes=data.scopes or ["*"],
        expires_at=expires_at,
    )
    session.add(token)
    await session.commit()
    await session.refresh(token)

    return ApiTokenCreated(
        **ApiTokenRead.model_validate(token).model_dump(),
        plaintext_token=plaintext,
        revealable=token.encrypted_secret is not None,
    )


@router.get("", response_model=list[ApiTokenRead])
async def list_tokens(
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_scope("tokens:read")),
) -> list[ApiTokenRead]:
    stmt = (
        select(ApiToken)
        .where(ApiToken.user_id == user.id)
        .order_by(ApiToken.created_at.desc())
    )
    result = await session.execute(stmt)
    return [ApiTokenRead.model_validate(t) for t in result.scalars().all()]


@router.post("/{token_id}/reveal", response_model=ApiTokenReveal)
async def reveal_token(
    token_id: UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_scope("tokens:read")),
) -> ApiTokenReveal:
    token = await session.get(ApiToken, token_id)
    if not token or token.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "令牌不存在")
    plaintext = decrypt_api_token(token.encrypted_secret)
    if not plaintext:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "该令牌创建时未启用加密存储，无法查看明文；请在创建后立即复制，或配置 POBI_V2_TOKEN_ENCRYPTION_KEY 后重新创建。",
        )
    return ApiTokenReveal(id=token.id, plaintext_token=plaintext)


@router.delete("/{token_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_token(
    token_id: UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_scope("tokens:write")),
) -> None:
    token = await session.get(ApiToken, token_id)
    if not token or token.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "令牌不存在")
    token.revoked = True
    await session.commit()
