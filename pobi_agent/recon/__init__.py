"""RECON 本地物化存储包。

提供 per-task SQLite 物化库（ReconStore）、模型定义（sqlite_models）、
secret 级加密占位（crypto）。供 ContextEngine 旁路写入、recon_lookup 工具
与利用阶段 L0/L1/L2 分层注入复用。
"""

from .crypto import ReconCrypto
from .sqlite_models import (
    ReconBase,
    ReconCategory,
    ReconEndpoint,
    ReconFact,
    ReconSession,
    ReconTechnique,
    ReconThreat,
    Sensitivity,
    ThreatStatus,
    normalize_category,
)
from .store import ReconStore, ReconStoreError

__all__ = [
    "ReconBase",
    "ReconCategory",
    "ReconEndpoint",
    "ReconFact",
    "ReconSession",
    "ReconTechnique",
    "ReconThreat",
    "ReconStore",
    "ReconStoreError",
    "ReconCrypto",
    "Sensitivity",
    "ThreatStatus",
    "normalize_category",
]
