"""RECON 本地存储 SQLAlchemy 模型（per-task SQLite）。

设计依据：docs/RECON_LOCAL_STORE_DESIGN.md §2-§4。

本模块定义本地物化库的 5 张表，与 RAG 库共用同一 SQLite 文件
（由 ReconStore 按 task_id 定位），但使用独立的 ``ReconBase`` declarative_base，
避免与 RAG 表（CodeChunkSqlite）产生外键耦合。

维度约定（v2.1 修订）：
- 本地库以 **task_id** 为隔离维度（每任务一个 .db 文件），
  单任务内可包含多个 target/主机，通过 task_id 列统一归属。
- 不引入租户概念（本地库为单 agent 进程视角）。
"""

from __future__ import annotations

import datetime as _dt
from enum import Enum
from typing import Any, Dict, List

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import declarative_base, relationship

# 独立的 declarative base，不污染 RAG 表的 Base。
ReconBase = declarative_base()


class ReconCategory(str, Enum):
    """事实类别（对应设计文档表 2-3）。

    与 ContextEngine 既有 fact category 约定保持一致
    （endpoint / parameter / filter / vulnerability / technology /
    auth_method / validated_exploit 等），同时支持 recon 扩展类别。
    """

    endpoint = "endpoint"
    parameter = "parameter"
    filter = "filter"
    vulnerability = "vulnerability"
    technology = "technology"
    auth_method = "auth_method"
    credential = "credential"
    authentication = "authentication"
    validated_exploit = "validated_exploit"
    finding = "finding"
    attack_vector = "attack_vector"
    feature = "feature"
    misc = "misc"

    @classmethod
    def values(cls) -> List[str]:
        return [m.value for m in cls]


class Sensitivity(str, Enum):
    """证据敏感度分级（设计文档 §4.2）。

    secret 级证据本次仅做分级标记，AES-GCM 加密由 crypto 模块占位，
    密钥管理留待 PG 同步阶段统一设计。
    """

    public = "public"
    internal = "internal"
    secret = "secret"
    critical = "critical"


class ThreatStatus(str, Enum):
    """威胁确认状态（设计文档 §3，recon_threats 表）。"""

    suspected = "suspected"
    confirmed = "confirmed"
    exploited = "exploited"
    remediated = "remediated"


# ---------------------------------------------------------------------------
# 归一化辅助
# ---------------------------------------------------------------------------

# ContextEngine 既有 fact category 用语 → ReconCategory 归一映射。
_CATEGORY_ALIASES: Dict[str, ReconCategory] = {
    "validated_exploit": ReconCategory.validated_exploit,
    "exploit": ReconCategory.validated_exploit,
    "vuln": ReconCategory.vulnerability,
    "tech": ReconCategory.technology,
    "auth": ReconCategory.auth_method,
    "creds": ReconCategory.credential,
}


def normalize_category(category: str | ReconCategory) -> ReconCategory:
    """将任意来源类别归一为 ReconCategory 枚举。

    Args:
        category: 字符串或 ReconCategory 实例。

    Returns:
        合法的 ReconCategory；无法识别时回退到 ``misc``。
    """
    if isinstance(category, ReconCategory):
        return category
    value = (category or "").strip().lower()
    if not value:
        return ReconCategory.misc
    if value in ReconCategory.values():
        return ReconCategory(value)
    return _CATEGORY_ALIASES.get(value, ReconCategory.misc)


def _now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


# ---------------------------------------------------------------------------
# 表定义
# ---------------------------------------------------------------------------


class ReconSession(ReconBase):
    """recon_sessions：任务级会话元数据（表 4-1）。"""

    __tablename__ = "recon_sessions"
    __table_args__ = (UniqueConstraint("task_id", name="uq_recon_sessions_task"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    task_id = Column(String(64), nullable=False, index=True)
    target = Column(String(512), nullable=False, default="")
    objective = Column(Text, nullable=False, default="")
    agent_session = Column(String(128), nullable=False, default="")
    status = Column(String(32), nullable=False, default="active")
    created_at = Column(DateTime, nullable=False, default=_now)
    updated_at = Column(DateTime, nullable=False, default=_now, onupdate=_now)
    schema_version = Column(Integer, nullable=False, default=1)
    meta_json = Column(JSON, nullable=False, default=dict)

    facts = relationship(
        "ReconFact",
        back_populates="session",
        cascade="all, delete-orphan",
    )
    endpoints = relationship(
        "ReconEndpoint",
        back_populates="session",
        cascade="all, delete-orphan",
    )
    techniques = relationship(
        "ReconTechnique",
        back_populates="session",
        cascade="all, delete-orphan",
    )
    threats = relationship(
        "ReconThreat",
        back_populates="session",
        cascade="all, delete-orphan",
    )
    fingerprints = relationship(
        "ReconFingerprint",
        back_populates="session",
        cascade="all, delete-orphan",
    )
    http_transactions = relationship(
        "ReconHttpTransaction",
        back_populates="session",
        cascade="all, delete-orphan",
    )


class ReconFact(ReconBase):
    """recon_facts：扁平事实宽表（表 4-2），幂等键 (task_id, category, key)。"""

    __tablename__ = "recon_facts"
    __table_args__ = (
        UniqueConstraint(
            "task_id", "category", "key", name="uq_recon_facts_task_cat_key"
        ),
        Index("ix_recon_facts_task_cat", "task_id", "category"),
        Index("ix_recon_facts_task_conf", "task_id", "confidence"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(
        Integer,
        ForeignKey("recon_sessions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    task_id = Column(String(64), nullable=False, index=True)
    category = Column(String(32), nullable=False)
    key = Column(String(512), nullable=False)
    value = Column(Text, nullable=False, default="")
    confidence = Column(Float, nullable=False, default=0.5)
    source = Column(String(128), nullable=False, default="")
    sensitivity = Column(String(16), nullable=False, default=Sensitivity.internal.value)
    details_json = Column(JSON, nullable=False, default=dict)
    # 大文本外置锚点：>100KB 证据写 blobs/，DB 仅存摘要。
    blob_ref = Column(String(256), nullable=True)
    created_at = Column(DateTime, nullable=False, default=_now)
    updated_at = Column(DateTime, nullable=False, default=_now, onupdate=_now)
    # PG 聚合层同步标记：NULL=待同步（脏），非 NULL=最近成功同步时间（增量游标）。
    pg_synced_at = Column(DateTime, nullable=True)

    session = relationship("ReconSession", back_populates="facts")


class ReconEndpoint(ReconBase):
    """recon_endpoints：端点/资产表（表 4-3），幂等键 (task_id, path_normalized)。"""

    __tablename__ = "recon_endpoints"
    __table_args__ = (
        UniqueConstraint(
            "task_id", "path_normalized", name="uq_recon_endpoints_task_path"
        ),
        Index("ix_recon_endpoints_task_host", "task_id", "host"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(
        Integer,
        ForeignKey("recon_sessions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    task_id = Column(String(64), nullable=False, index=True)
    host = Column(String(255), nullable=False, default="")
    path_normalized = Column(String(512), nullable=False)
    method = Column(String(16), nullable=False, default="GET")
    status_code = Column(Integer, nullable=True)
    auth_required = Column(Boolean, nullable=False, default=False)
    tech_stack = Column(JSON, nullable=False, default=list)
    parameters = Column(JSON, nullable=False, default=list)
    notes = Column(Text, nullable=False, default="")
    discovered_via = Column(String(128), nullable=False, default="")
    confidence = Column(Float, nullable=False, default=0.7)
    created_at = Column(DateTime, nullable=False, default=_now)
    updated_at = Column(DateTime, nullable=False, default=_now, onupdate=_now)
    # PG 聚合层同步标记：NULL=待同步（脏），非 NULL=最近成功同步时间（增量游标）。
    pg_synced_at = Column(DateTime, nullable=True)

    session = relationship("ReconSession", back_populates="endpoints")


class ReconTechnique(ReconBase):
    """recon_techniques：测试技术栈表（表 4-4），幂等键 (task_id, name)。"""

    __tablename__ = "recon_techniques"
    __table_args__ = (
        UniqueConstraint("task_id", "name", name="uq_recon_techniques_task_name"),
        Index("ix_recon_techniques_task_status", "task_id", "status"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(
        Integer,
        ForeignKey("recon_sessions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    task_id = Column(String(64), nullable=False, index=True)
    name = Column(String(128), nullable=False)
    category = Column(String(64), nullable=False, default="")
    status = Column(String(32), nullable=False, default="untested")
    success_count = Column(Integer, nullable=False, default=0)
    tested_count = Column(Integer, nullable=False, default=0)
    last_result = Column(Text, nullable=False, default="")
    confidence = Column(Float, nullable=False, default=0.5)
    created_at = Column(DateTime, nullable=False, default=_now)
    updated_at = Column(DateTime, nullable=False, default=_now, onupdate=_now)

    session = relationship("ReconSession", back_populates="techniques")


class ReconThreat(ReconBase):
    """recon_threats：威胁/漏洞表（表 4-5），幂等键 (task_id, cve_id)。"""

    __tablename__ = "recon_threats"
    __table_args__ = (
        UniqueConstraint("task_id", "cve_id", name="uq_recon_threats_task_cve"),
        Index("ix_recon_threats_task_status", "task_id", "status"),
        Index("ix_recon_threats_task_sev", "task_id", "severity"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(
        Integer,
        ForeignKey("recon_sessions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    task_id = Column(String(64), nullable=False, index=True)
    cve_id = Column(String(64), nullable=False, default="")
    title = Column(String(512), nullable=False, default="")
    category = Column(String(64), nullable=False, default="")
    severity = Column(String(16), nullable=False, default="medium")
    status = Column(String(16), nullable=False, default=ThreatStatus.suspected.value)
    cvss_score = Column(Float, nullable=True)
    affected_endpoint = Column(String(512), nullable=False, default="")
    # secret 级证据（payload / exploit）外置加密，DB 仅存摘要。
    evidence_blob_ref = Column(String(256), nullable=True)
    evidence_summary = Column(Text, nullable=False, default="")
    confidence = Column(Float, nullable=False, default=0.5)
    created_at = Column(DateTime, nullable=False, default=_now)
    updated_at = Column(DateTime, nullable=False, default=_now, onupdate=_now)
    # PG 聚合层同步标记：NULL=待同步（脏），非 NULL=最近成功同步时间（增量游标）。
    pg_synced_at = Column(DateTime, nullable=True)

    session = relationship("ReconSession", back_populates="threats")


class ReconHttpTransaction(ReconBase):
    """recon_http_transactions：HTTP 请求/响应事务流水（前置侦查 sitemap + requester 共用）。

    对齐 katana JSONL 与 requester 请求的统一 schema，供站点地图按端点聚合展示
    （URL 树 + 方法/状态码徽标 + 每个端点的请求历史对比）。

    无幂等键：每次请求一条流水（保留历史可对比，如 katana 403 vs requester 带
    cookie 200 → 认证差异一目了然）。去重/去噪在 recon_endpoints 端点树完成。
    ``source`` 标记来源（sitemap:katana / agent:requester），站点地图按来源着色。

    响应体分层存储（2026-09-02）：
    - ``storage_strategy``：full（明文全量落库）/ compressed（gzip 压缩存
      ``body_compressed`` BLOB）/ digest（仅存前 ``_DIGEST_PREFIX_BYTES`` 摘要）。
    - json/xml API 响应即使大也走 compressed（全量保留，压缩存储）。
    - 读取侧按需拉取：列表默认骨架（不拖大 body），完整内容经
      ``ReconStore.get_transaction_body`` 解压/取全文。
    """

    __tablename__ = "recon_http_transactions"
    __table_args__ = (
        Index("ix_http_tx_task_host_path", "task_id", "host", "path_normalized"),
        Index("ix_http_tx_task_source", "task_id", "source"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(
        Integer,
        ForeignKey("recon_sessions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    task_id = Column(String(64), nullable=False, index=True)
    host = Column(String(255), nullable=False, default="")
    path_normalized = Column(String(512), nullable=False, default="")
    url = Column(String(1024), nullable=False, default="")
    method = Column(String(16), nullable=False, default="GET")
    status_code = Column(Integer, nullable=True)
    source = Column(String(32), nullable=False, default="sitemap:katana")
    # 请求侧
    request_headers = Column(JSON, nullable=False, default=dict)
    request_body = Column(Text, nullable=False, default="")
    # 响应侧
    response_headers = Column(JSON, nullable=False, default=dict)
    response_body = Column(Text, nullable=False, default="")
    # 分层存储（2026-09-02）：body_compressed 为 gzip 压缩字节（strategy=compressed 时）。
    storage_strategy = Column(String(16), nullable=False, default="full")
    body_compressed = Column(LargeBinary, nullable=True)
    body_blob_ref = Column(String(256), nullable=True)
    response_title = Column(String(512), nullable=False, default="")
    content_type = Column(String(128), nullable=False, default="")
    response_size = Column(Integer, nullable=False, default=0)
    response_time_ms = Column(Integer, nullable=True)
    tech_stack = Column(JSON, nullable=False, default=list)
    detected_params = Column(JSON, nullable=False, default=list)
    detected_forms = Column(JSON, nullable=False, default=list)
    # 认证感知
    auth_used = Column(Boolean, nullable=False, default=False)
    auth_required = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, nullable=False, default=_now)
    # PG 聚合层同步标记：NULL=待同步（脏），非 NULL=最近成功同步时间（增量游标）。
    pg_synced_at = Column(DateTime, nullable=True)

    session = relationship("ReconSession", back_populates="http_transactions")


class ReconFingerprint(ReconBase):
    """recon_fingerprints：指纹识别明细表（前置侦查专用）。

    幂等键 (task_id, target_url)：同 host 一次扫描，重复落库仅更新时间。
    与 recon_facts(technology)/recon_endpoints(tech_stack) 互补：
    本表存完整结构化明细（四层指纹 + favicon + 匹配证据），
    facts/endpoints 承载供 L0/L1 注入的精炼结论。
    """

    __tablename__ = "recon_fingerprints"
    __table_args__ = (
        UniqueConstraint(
            "task_id", "target_url", name="uq_recon_fingerprints_task_target"
        ),
        Index("ix_recon_fingerprints_task_host", "task_id", "host"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(
        Integer,
        ForeignKey("recon_sessions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    task_id = Column(String(64), nullable=False, index=True)
    target_url = Column(String(512), nullable=False)
    host = Column(String(255), nullable=False, default="")
    status_code = Column(Integer, nullable=True)
    favicon_hash = Column(BigInteger, nullable=True)
    auth_mode = Column(String(32), nullable=False, default="external_only")
    # 四层指纹结果：{"server":[], "backend":[], "frontend":[], "cms":[], "waf":[]}
    fingerprint_json = Column(JSON, nullable=False, default=dict)
    matched_count = Column(Integer, nullable=False, default=0)
    rule_count = Column(Integer, nullable=False, default=0)
    source = Column(String(128), nullable=False, default="pre_recon")
    created_at = Column(DateTime, nullable=False, default=_now)
    updated_at = Column(DateTime, nullable=False, default=_now, onupdate=_now)
    # PG 聚合层同步标记：NULL=待同步（脏），非 NULL=最近成功同步时间（增量游标）。
    pg_synced_at = Column(DateTime, nullable=True)

    session = relationship("ReconSession", back_populates="fingerprints")
