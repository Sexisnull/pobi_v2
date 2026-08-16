"""M4 鉴权核心：密码哈希、JWT 签发/校验。

使用 PyJWT + bcrypt。配置项读取自 settings（POBI_V2_* 环境变量）。
"""
from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
import bcrypt
import jwt
from jwt.exceptions import ExpiredSignatureError, InvalidTokenError

from pobi_v2.core.config import settings

ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24  # 1 天

# ── 个人访问令牌（PAT）相关 ──
# 明文格式：pk_<前缀随机8位>_<48位随机>，前缀仅用于展示识别。
API_TOKEN_PREFIX_HDR = "pk"
PAT_KEY_ENV = "POBI_TOKEN_KEY"


def _pat_fernet() -> Fernet | None:
    """用 POBI_TOKEN_KEY 派生 Fernet 密钥；未配置时返回 None（不可 reveal）。"""
    raw = getattr(settings, "token_encryption_key", None) or ""
    if not raw:
        return None
    # Fernet 需要 32 字节 url-safe base64 密钥：从任意字符串派生
    import base64

    digest = hashlib.sha256(raw.encode("utf-8")).digest()
    key = base64.urlsafe_b64encode(digest)
    return Fernet(key)


def generate_api_token() -> tuple[str, str, str]:
    """生成 PAT。

    返回 (plaintext, prefix, sha256_hex)：
    - plaintext：完整明文令牌，仅创建时返回一次，可点击查看的前提是后端有加密密钥。
    - prefix：明文前缀（pk_xxxx），用于列表识别。
    - sha256_hex：明文 SHA-256，用于校验，不可逆。
    """
    rand = secrets.token_urlsafe(48).replace("-", "").replace("_", "")[:48]
    prefix_rand = secrets.token_hex(4)
    plaintext = f"{API_TOKEN_PREFIX_HDR}_{prefix_rand}_{rand}"
    prefix = f"{API_TOKEN_PREFIX_HDR}_{prefix_rand}"
    sha = hashlib.sha256(plaintext.encode("utf-8")).hexdigest()
    return plaintext, prefix, sha


def hash_api_token(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def encrypt_api_token(plaintext: str) -> str | None:
    """加密明文以便后续 reveal；无密钥时返回 None（创建时仍能看到明文）。"""
    f = _pat_fernet()
    if f is None:
        return None
    return f.encrypt(plaintext.encode("utf-8")).decode("utf-8")


def decrypt_api_token(encrypted: str | None) -> str | None:
    """解密明文令牌供点击查看；无密钥或解密失败返回 None。"""
    if not encrypted:
        return None
    f = _pat_fernet()
    if f is None:
        return None
    try:
        return f.decrypt(encrypted.encode("utf-8")).decode("utf-8")
    except (InvalidToken, Exception):
        return None


def verify_api_token(plaintext: str, token_hash: str) -> bool:
    return secrets.compare_digest(hash_api_token(plaintext), token_hash)


def hash_password(password: str) -> str:
    """bcrypt 哈希。"""
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except ValueError:
        return False


def create_access_token(subject: str, tenant_id: str, extra: dict | None = None) -> str:
    """签发 JWT（payload 含 sub / tenant_id / 过期时间）。"""
    now = datetime.now(timezone.utc)
    payload: dict[str, Any] = {
        "sub": subject,
        "tenant_id": tenant_id,
        "iat": now,
        "exp": now + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES),
    }
    if extra:
        payload.update(extra)
    return jwt.encode(payload, settings.jwt_secret, algorithm=ALGORITHM)


def decode_access_token(token: str) -> dict[str, Any]:
    """校验 JWT，返回 payload；失败抛 InvalidTokenError / ExpiredSignatureError。"""
    return jwt.decode(token, settings.jwt_secret, algorithms=[ALGORITHM])
