"""RECON secret 级证据加密占位接口（设计文档 §4.2）。

本次仅暴露 ``encrypt`` / ``decrypt`` 签名与 Fernet 封装，密钥管理
（KMS / 环境变量 / PG 同步阶段统一派生）留待后续阶段，不阻塞本地物化落地。

本地库默认关闭加密（``ENABLED = False``）。当 ``RECON_SECRET_KEY`` 环境变量
可用时自动启用；密钥缺失时不抛错，降级为明文存储并记 warning，保证 agent
主循环不被密钥问题阻断。
"""

from __future__ import annotations

import base64
import logging
import os
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)

# 密钥环境变量名（占位；正式密钥策略在 PG 同步阶段设计）。
_SECRET_ENV = "RECON_SECRET_KEY"

# 单次加密明文上限（100KB），超出应外置 blobs/ 仅存摘要。
_MAX_PLAINTEXT_BYTES = 100 * 1024


class ReconCrypto:
    """secret 级证据 AES-GCM 加密封装（Fernet）。

    非线程安全，但 SQLite 本地库为单进程单连接，调用方应保证串行访问。
    """

    def __init__(self, secret_key: Optional[bytes] = None) -> None:
        """初始化加密器。

        Args:
            secret_key: 32 字节 URL-safe base64 编码的 Fernet key。
                缺省时从环境变量 ``RECON_SECRET_KEY`` 读取；均缺失则禁用加密。
        """
        self.enabled = False
        self._fernet: Optional[Fernet] = None
        raw = secret_key or self._read_env_key()
        if raw:
            try:
                self._fernet = Fernet(raw)
                self.enabled = True
            except Exception as exc:  # noqa: BLE001 - 降级而非阻断
                logger.warning("RECON 加密初始化失败，降级为明文存储: %s", exc)
                self.enabled = False

    @staticmethod
    def _read_env_key() -> Optional[bytes]:
        val = os.environ.get(_SECRET_ENV)
        if not val:
            return None
        try:
            return val.encode("ascii") if _is_valid_fernet_key(val) else None
        except Exception:  # noqa: BLE001
            return None

    def encrypt(self, plaintext: str) -> str:
        """加密明文，返回 token 字符串。

        未启用时原样返回（明文降级）。超出上限时抛 ``ValueError``，
        调用方应改为外置 blobs/ 存储。
        """
        data = plaintext.encode("utf-8")
        if len(data) > _MAX_PLAINTEXT_BYTES:
            raise ValueError(
                f"明文超过 {_MAX_PLAINTEXT_BYTES} 字节上限，请外置 blobs/ 存储"
            )
        if not self.enabled or self._fernet is None:
            return plaintext
        return self._fernet.encrypt(data).decode("ascii")

    def decrypt(self, token: str) -> str:
        """解密 token，返回明文。明文降级模式下原样返回。"""
        if not self.enabled or self._fernet is None:
            return token
        try:
            return self._fernet.decrypt(token.encode("ascii")).decode("utf-8")
        except InvalidToken as exc:
            raise ValueError("RECON 证据解密失败：token 无效或密钥不匹配") from exc

    @staticmethod
    def generate_key() -> str:
        """生成一个新的 Fernet key（供运维写入环境变量使用）。"""
        return Fernet.generate_key().decode("ascii")


def _is_valid_fernet_key(val: str) -> bool:
    try:
        base64.urlsafe_b64decode(val)
        return len(val) == 44  # Fernet key 固定长度
    except Exception:  # noqa: BLE001
        return False
