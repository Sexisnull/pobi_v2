"""ReconStore：RECON 本地 SQLite 物化库读写层（设计文档 §4）。

职责单一：管理 per-task SQLite 连接生命周期（WAL / 外键 / user_version 迁移），
屏蔽 SQLAlchemy 细节，向上层（ContextEngine 旁路写入、recon_lookup 工具、
利用阶段 L0/L1/L2 注入）提供幂等 upsert、分层索引构建与精确检索。

接口与未来 PG 聚合层对称（``upsert_to_pg`` 为预留扩展点），事件钩子
``PobiV2EventHooks`` 可未来订阅落库事件，本次不实现同步。

数据库路径约定（v2.1 per-task）：
    {task_root}/{task_id}.db
其中 task_root 来自 ``pobi_agent.storage_context`` 协程级上下文。
侦察与利用阶段产物共用此单一库，以不同表区分。
"""

from __future__ import annotations

import gzip
import logging
import os
import re
from dataclasses import dataclass, field
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy import event
from sqlalchemy.exc import IntegrityError

from pobi_agent.logging import get_module_logger
from pobi_agent.utils.functions import num_tokens_from_string

from .crypto import ReconCrypto
from .sqlite_models import (
    ReconBase,
    ReconCategory,
    ReconEndpoint,
    ReconFact,
    ReconFingerprint,
    ReconHttpTransaction,
    ReconSession,
    ReconTechnique,
    ReconThreat,
    Sensitivity,
    ThreatStatus,
    normalize_category,
)

logger = get_module_logger(__name__)

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

# 本地库 schema 版本（user_version 迁移用）。
SCHEMA_VERSION = 1

# 分层索引 token 预算（设计文档 L1<500、L2<1500）。
L1_TOKEN_BUDGET = 500
L2_TOKEN_BUDGET = 1500

# WAL 与连接加固。
_WAL_PRAGMAS = (
    "PRAGMA journal_mode=WAL;",
    "PRAGMA synchronous=NORMAL;",
    "PRAGMA foreign_keys=ON;",
    "PRAGMA busy_timeout=5000;",
)

# 响应体分层存储阈值（2026-09-02）：
# - <100KB：full（明文全量落库）
# - 100KB–1MB：compressed（gzip 压缩存 body_compressed BLOB）
# - >1MB：digest（仅存前 _DIGEST_PREFIX_BYTES 字符摘要，不写外置）
# - json/xml API 响应例外：即使大也全量保留（≥100KB 走 compressed 压缩存储）。
_BLOB_THRESHOLD_BYTES = 100 * 1024
_COMPRESS_MAX_BYTES = 1024 * 1024
_DIGEST_PREFIX_BYTES = 2048
_PREVIEW_BYTES = 200
# API 响应判定：content-type 命中 json/xml（含 +json/+xml 变体）。
_API_CONTENT_TYPE_RE = re.compile(r"(?:json|xml)", re.IGNORECASE)

# 端点分类辅助：静态资源后缀 / API 路径特征（build_pre_recon_json 端点综述使用）。
_STATIC_ASSET_SUFFIXES = (
    ".css", ".js", ".mjs", ".png", ".jpg", ".jpeg", ".gif", ".svg",
    ".ico", ".webp", ".woff", ".woff2", ".ttf", ".eot", ".map",
    ".pdf", ".zip", ".gz", ".tar", ".mp4", ".webm", ".mp3",
)
_API_PATH_MARKERS = (
    "/api/", "/rest/", "/graphql", "/v1/", "/v2/", "/openapi",
    "/swagger", "/actuator", ".json", ".xml",
)


def _looks_like_static_asset(path: str) -> bool:
    """按路径后缀粗判静态资源端点（供综述统计，非精确分类）。"""
    p = path.lower()
    return p.endswith(_STATIC_ASSET_SUFFIXES) or "/static/" in p or "/assets/" in p


def _looks_like_api_path(path: str) -> bool:
    """按路径特征粗判 API 端点（供综述统计，非精确分类）。"""
    p = path.lower()
    return any(m in p for m in _API_PATH_MARKERS)


class ReconStoreError(Exception):
    """ReconStore 操作失败（非致命，调用方应忽略以不阻断 agent 主循环）。"""


# 威胁状态机单向顺序（设计文档 §5.3）：suspected → confirmed → exploited → remediated。
_THREAT_STATUS_ORDER: Dict[str, int] = {
    ThreatStatus.suspected.value: 0,
    ThreatStatus.confirmed.value: 1,
    ThreatStatus.exploited.value: 2,
    ThreatStatus.remediated.value: 3,
}


@dataclass
class SeedResult:
    """seed_from_pg / covered_assets 的覆盖清单结果。

    - seeded_count: 本次新灌入本地库的记录数（首次出现）。
    - already_covered_count: 已存在、未被重复灌入的记录数。
    - covered_endpoints: 已覆盖端点路径（历史任务已发现）。
    - covered_techniques: 已覆盖技术栈（来自端点 tech_stack 聚合）。
    - covered_threats: 已确认/已利用的威胁标识（cve_id 或 title）。
    """

    seeded_count: int = 0
    already_covered_count: int = 0
    covered_endpoints: List[str] = field(default_factory=list)
    covered_techniques: List[str] = field(default_factory=list)
    covered_threats: List[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return self.seeded_count > 0 or self.already_covered_count > 0


def _status_rank(code: Optional[int]) -> int:
    """状态码优劣秩：2xx > 3xx > 4xx > 5xx（用于端点派生时择优）。

    返回越高表示状态越好，故 2xx→3, 3xx→2, 4xx→1, 5xx→0, None→-1。
    """
    if code is None:
        return -1
    return 5 - code // 100


def _latest_session_id(db, task_id: str) -> Optional[int]:
    """取该 task 最新 session id（派生端点写回时需要非空 session_id）。"""
    sess = (
        db.query(ReconSession)
        .filter(ReconSession.task_id == task_id)
        .order_by(ReconSession.id.desc())
        .first()
    )
    return sess.id if sess else None


class ReconStore:
    """per-task RECON 本地物化库读写层。

    用法：
        store = ReconStore.for_task(task_id, task_root)
        store.ensure_session(target, objective)
        store.upsert_fact("endpoint", "/admin", "login panel")
        view = store.build_index_view(objective)
    """

    def __init__(
        self,
        db_path: Path,
        crypto: Optional[ReconCrypto] = None,
    ) -> None:
        self.db_path = db_path
        self.crypto = crypto or ReconCrypto()
        self._engine = create_engine(
            f"sqlite:///{db_path}",
            future=True,
            connect_args={"check_same_thread": False},
        )
        self._session_factory = sessionmaker(
            bind=self._engine, future=True, expire_on_commit=False
        )
        # foreign_keys 是连接级 pragma，必须在每个新连接上设置（不随 WAL 持久化）。
        event.listen(self._engine, "connect", self._on_connect)
        self._apply_pragmas()
        self._create_tables()
        self._migrate()

    @staticmethod
    def _on_connect(dbapi_conn, _record) -> None:
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    # ------------------------------------------------------------------
    # 工厂与连接管理
    # ------------------------------------------------------------------

    @classmethod
    def for_task(
        cls,
        task_id: str,
        task_root: Optional[str] = None,
        crypto: Optional[ReconCrypto] = None,
    ) -> "ReconStore":
        """为指定任务构造 Store，定位到 {task_root}/{task_id}.db（任务级单一库）。

        侦察与利用阶段产物共用此库，以不同表区分。路径做规范化与越界校验，
        禁止 ``..`` 逃逸出 task_root。
        """
        if not task_id:
            raise ReconStoreError("task_id 不能为空")
        safe_id = _sanitize_task_id(task_id)
        if task_root:
            root = Path(task_root).resolve()
            # 任务级单一库：tasks/<id>/<id>.db（侦察/利用产物同库不同表）。
            root.mkdir(parents=True, exist_ok=True)
            db_path = root / f"{safe_id}.db"
            try:
                db_path.resolve().relative_to(root)
            except ValueError as exc:
                raise ReconStoreError("数据库路径越界 task_root") from exc
        else:
            # 无 task_root 时落到工作目录（测试/CLI 场景）。
            recon_dir = Path.cwd() / "recon_local"
            recon_dir.mkdir(parents=True, exist_ok=True)
            db_path = recon_dir / f"{safe_id}.db"
        # 收敛 .db 文件权限（仅属主可读写）。
        _restrict_file_perms(db_path)
        return cls(db_path, crypto=crypto)

    def _apply_pragmas(self) -> None:
        with self._engine.connect() as conn:
            try:
                for pragma in _WAL_PRAGMAS:
                    conn.exec_driver_sql(pragma)
            except Exception:  # noqa: BLE001
                # 部分文件系统（如容器 overlayfs）不支持 WAL 模式：-wal/-shm 的
                # mmap/原子 rename 失败 -> disk I/O error。回退到兼容性最佳的
                # DELETE（rollback journal）模式，避免建连即失败导致 recon 本地库不可用。
                for fallback in (
                    "PRAGMA journal_mode=DELETE;",
                    "PRAGMA synchronous=NORMAL;",
                    "PRAGMA busy_timeout=5000;",
                ):
                    try:
                        conn.exec_driver_sql(fallback)
                    except Exception:  # noqa: BLE001
                        pass
            conn.commit()

    def _create_tables(self) -> None:
        ReconBase.metadata.create_all(self._engine)
        # FTS5 辅助检索（v2.1 修订点 #4）：为 recon_facts 建全文索引虚拟表。
        with self._engine.connect() as conn:
            conn.exec_driver_sql(
                "CREATE VIRTUAL TABLE IF NOT EXISTS recon_facts_fts "
                "USING fts5(key, value, content='recon_facts', content_rowid='id')"
            )
            conn.commit()

    def _migrate(self) -> None:
        """基于 user_version 的简单迁移框架（本次仅 v1）。"""
        with self._engine.connect() as conn:
            current = conn.exec_driver_sql("PRAGMA user_version").scalar() or 0
            if current < SCHEMA_VERSION:
                conn.exec_driver_sql(f"PRAGMA user_version={SCHEMA_VERSION}")
                conn.commit()
        # 幂等加列：PG 增量同步游标 pg_synced_at（旧库缺列时 ALTER TABLE 补齐）。
        for table in ("recon_facts", "recon_endpoints", "recon_threats"):
            self._ensure_column(table, "pg_synced_at")
        # 分层存储列（2026-09-02）：历史 recon_http_transactions 库补齐。
        self._ensure_column("recon_http_transactions", "pg_synced_at", "DATETIME")
        self._ensure_column("recon_http_transactions", "storage_strategy", "VARCHAR(16)")
        self._ensure_column("recon_http_transactions", "body_compressed", "BLOB")

    def _ensure_column(self, table: str, column: str, col_type: str = "DATETIME") -> None:
        """检查表是否存在指定列，缺失则 ALTER TABLE ADD COLUMN（幂等）。

        新库经 ``metadata.create_all`` 已含全部列；仅历史库需要补齐。
        """
        with self._engine.connect() as conn:
            rows = conn.exec_driver_sql(f"PRAGMA table_info({table})").fetchall()
            if column not in {r[1] for r in rows}:
                conn.exec_driver_sql(
                    f"ALTER TABLE {table} ADD COLUMN {column} {col_type}"
                )
                conn.commit()

    def close(self) -> None:
        """关闭引擎，释放连接。"""
        try:
            self._engine.dispose()
        except Exception as exc:  # noqa: BLE001
            logger.warning("ReconStore 关闭异常: %s", exc)

    # ------------------------------------------------------------------
    # 会话元数据
    # ------------------------------------------------------------------

    def ensure_session(
        self,
        task_id: str,
        target: str = "",
        objective: str = "",
        agent_session: str = "",
    ) -> int:
        """确保存在 task 级 recon_sessions 记录，返回 session_id。

        幂等：已存在则更新 updated_at 并返回既有 id。
        """
        with self._session_factory() as session:
            existing = session.execute(
                select(ReconSession).where(ReconSession.task_id == task_id)
            ).scalar_one_or_none()
            if existing:
                existing.updated_at = _utcnow()
                if target and not existing.target:
                    existing.target = target
                if objective and not existing.objective:
                    existing.objective = objective
                session.commit()
                return existing.id
            rec = ReconSession(
                task_id=task_id,
                target=target,
                objective=objective,
                agent_session=agent_session,
                schema_version=SCHEMA_VERSION,
            )
            session.add(rec)
            session.commit()
            return rec.id

    # ------------------------------------------------------------------
    # 幂等 upsert
    # ------------------------------------------------------------------

    def upsert_fact(
        self,
        task_id: str,
        category: str | ReconCategory,
        key: str,
        value: str,
        confidence: float = 0.5,
        source: str = "",
        sensitivity: Sensitivity | str = Sensitivity.internal,
        details: Optional[Dict[str, Any]] = None,
        session_id: Optional[int] = None,
    ) -> None:
        """幂等写入 recon_facts，冲突键 (task_id, category, key)。

        冲突时仅在 candidate confidence 更高时覆盖（与 ContextEngine 内存
        去重策略一致）。
        """
        norm_cat = normalize_category(category).value
        sens = sensitivity.value if isinstance(sensitivity, Sensitivity) else str(sensitivity)
        if session_id is None:
            session_id = self.ensure_session(task_id)
        blob_ref = None
        if len(value.encode("utf-8")) > _BLOB_THRESHOLD_BYTES:
            blob_ref = self._write_blob(task_id, key, value)
            value = value[:200]  # 入库保留摘要
        with self._session_factory() as session:
            existing = session.execute(
                select(ReconFact).where(
                    ReconFact.task_id == task_id,
                    ReconFact.category == norm_cat,
                    ReconFact.key == key,
                )
            ).scalar_one_or_none()
            if existing:
                # 任何更新都置脏（pg_synced_at=NULL），保证增量同步不遗漏。
                existing.pg_synced_at = None
                if confidence > existing.confidence:
                    existing.value = value
                    existing.confidence = confidence
                    existing.source = source
                    existing.sensitivity = sens
                    existing.details_json = details or {}
                    existing.blob_ref = blob_ref
                    existing.updated_at = _utcnow()
                else:
                    # 低置信度候选不覆盖，仅补充细节。
                    if details:
                        merged = dict(existing.details_json or {})
                        merged.update(details)
                        existing.details_json = merged
                fact_id = existing.id
            else:
                rec = ReconFact(
                    session_id=session_id,
                    task_id=task_id,
                    category=norm_cat,
                    key=key,
                    value=value,
                    confidence=confidence,
                    source=source,
                    sensitivity=sens,
                    details_json=details or {},
                    blob_ref=blob_ref,
                )
                session.add(rec)
                session.flush()
                fact_id = rec.id
            session.commit()
        # 同步 FTS5 外部内容索引（新增/更新均替换行）。
        self._sync_fts(fact_id, key, value)

    def upsert_endpoint(
        self,
        task_id: str,
        path_normalized: str,
        host: str = "",
        method: str = "GET",
        status_code: Optional[int] = None,
        auth_required: bool = False,
        tech_stack: Optional[List[str]] = None,
        parameters: Optional[List[str]] = None,
        notes: str = "",
        discovered_via: str = "",
        confidence: float = 0.7,
        session_id: Optional[int] = None,
    ) -> None:
        """幂等写入 recon_endpoints，冲突键 (task_id, path_normalized)。"""
        if session_id is None:
            session_id = self.ensure_session(task_id)
        with self._session_factory() as session:
            existing = session.execute(
                select(ReconEndpoint).where(
                    ReconEndpoint.task_id == task_id,
                    ReconEndpoint.path_normalized == path_normalized,
                )
            ).scalar_one_or_none()
            if existing:
                # 任何更新都置脏（pg_synced_at=NULL），保证增量同步不遗漏。
                existing.pg_synced_at = None
                # 合并技术栈与参数，置信度取高。
                if tech_stack:
                    merged = list(existing.tech_stack or [])
                    for t in tech_stack:
                        if t not in merged:
                            merged.append(t)
                    existing.tech_stack = merged
                if parameters:
                    merged = list(existing.parameters or [])
                    for p in parameters:
                        if p not in merged:
                            merged.append(p)
                    existing.parameters = merged
                if confidence > existing.confidence:
                    existing.confidence = confidence
                    if status_code is not None:
                        existing.status_code = status_code
                    existing.auth_required = auth_required or existing.auth_required
                existing.updated_at = _utcnow()
            else:
                session.add(
                    ReconEndpoint(
                        session_id=session_id,
                        task_id=task_id,
                        host=host,
                        path_normalized=path_normalized,
                        method=method,
                        status_code=status_code,
                        auth_required=auth_required,
                        tech_stack=tech_stack or [],
                        parameters=parameters or [],
                        notes=notes,
                        discovered_via=discovered_via,
                        confidence=confidence,
                    )
                )
            session.commit()

    def upsert_technique(
        self,
        task_id: str,
        name: str,
        category: str = "",
        status: str = "untested",
        success_count: int = 0,
        tested_count: int = 0,
        last_result: str = "",
        confidence: float = 0.5,
        session_id: Optional[int] = None,
    ) -> None:
        """幂等写入 recon_techniques，冲突键 (task_id, name)。"""
        if session_id is None:
            session_id = self.ensure_session(task_id)
        with self._session_factory() as session:
            existing = session.execute(
                select(ReconTechnique).where(
                    ReconTechnique.task_id == task_id,
                    ReconTechnique.name == name,
                )
            ).scalar_one_or_none()
            if existing:
                # 计数/结果变化都置脏（pg_synced_at=NULL），保证增量同步不遗漏。
                existing.pg_synced_at = None
                existing.success_count += success_count
                existing.tested_count += tested_count
                if last_result:
                    existing.last_result = last_result
                if status != "untested":
                    existing.status = status
                if confidence > existing.confidence:
                    existing.confidence = confidence
                existing.updated_at = _utcnow()
            else:
                session.add(
                    ReconTechnique(
                        session_id=session_id,
                        task_id=task_id,
                        name=name,
                        category=category,
                        status=status,
                        success_count=success_count,
                        tested_count=tested_count,
                        last_result=last_result,
                        confidence=confidence,
                    )
                )
            session.commit()

    def upsert_threat(
        self,
        task_id: str,
        cve_id: str = "",
        title: str = "",
        category: str = "",
        severity: str = "medium",
        status: ThreatStatus | str = ThreatStatus.suspected,
        cvss_score: Optional[float] = None,
        affected_endpoint: str = "",
        evidence_summary: str = "",
        confidence: float = 0.5,
        sensitivity: Sensitivity | str = Sensitivity.internal,
        session_id: Optional[int] = None,
    ) -> None:
        """幂等写入 recon_threats，冲突键 (task_id, cve_id)。

        secret 级 evidence 经 crypto 加密占位（本次默认明文降级）。
        """
        if session_id is None:
            session_id = self.ensure_session(task_id)
        st = status.value if isinstance(status, ThreatStatus) else str(status)
        sens = sensitivity.value if isinstance(sensitivity, Sensitivity) else str(sensitivity)
        encrypted_ev = self.crypto.encrypt(evidence_summary) if evidence_summary else ""
        with self._session_factory() as session:
            existing = session.execute(
                select(ReconThreat).where(
                    ReconThreat.task_id == task_id,
                    ReconThreat.cve_id == cve_id,
                )
            ).scalar_one_or_none()
            if existing:
                # 任何更新都置脏（pg_synced_at=NULL），保证增量同步不遗漏。
                existing.pg_synced_at = None
                if confidence > existing.confidence:
                    existing.severity = severity
                    existing.status = st
                    existing.evidence_summary = encrypted_ev
                    existing.confidence = confidence
                if category and not existing.category:
                    existing.category = category
                existing.updated_at = _utcnow()
            else:
                session.add(
                    ReconThreat(
                        session_id=session_id,
                        task_id=task_id,
                        cve_id=cve_id,
                        title=title,
                        category=category,
                        severity=severity,
                        status=st,
                        cvss_score=cvss_score,
                        affected_endpoint=affected_endpoint,
                        evidence_summary=encrypted_ev,
                        confidence=confidence,
                    )
                )
            session.commit()

    # ------------------------------------------------------------------
    # 检索
    # ------------------------------------------------------------------

    def lookup(
        self,
        task_id: str,
        host: Optional[str] = None,
        tech: Optional[str] = None,
        path_prefix: Optional[str] = None,
        category: Optional[str] = None,
        keyword: Optional[str] = None,
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        """跨表精确/前缀/全文检索（设计文档 recon_lookup 工具底层）。

        命中规则：
        - host 命中 recon_endpoints.host
        - tech 命中 recon_endpoints.tech_stack 或 recon_techniques.name
        - path_prefix 命中 recon_endpoints.path_normalized 前缀
        - category 命中 recon_facts.category
        - keyword 走 FTS5 MATCH（v2.1 修订点 #4），增强模糊召回

        返回按置信度降序的归一化 dict 列表（FTS5 命中去重合并）。
        """
        results: List[Dict[str, Any]] = []
        seen_keys: set = set()
        with self._session_factory() as session:
            # FTS5 全文检索（v2.1 修订点 #4）：先取命中 rowid，再回表。
            fts_ids: set = set()
            if keyword:
                try:
                    rows = session.execute(
                        __import__("sqlalchemy").text(
                            "SELECT rowid FROM recon_facts_fts WHERE recon_facts_fts MATCH :kw"
                        ).bindparams(kw=self._fts_query(keyword))
                    ).fetchall()
                    fts_ids = {r[0] for r in rows}
                except Exception as exc:  # noqa: BLE001 - FTS 不可用时不阻断
                    logger.warning("RECON FTS5 查询失败（回退普通检索）: %s", exc)

            if category or host or path_prefix or keyword:
                stmt = select(ReconFact).where(ReconFact.task_id == task_id)
                if category:
                    stmt = stmt.where(ReconFact.category == normalize_category(category).value)
                if fts_ids:
                    stmt = stmt.where(ReconFact.id.in_(fts_ids))
                facts = session.execute(stmt).scalars().all()
                for f in facts:
                    results.append(
                        {
                            "kind": "fact",
                            "category": f.category,
                            "key": f.key,
                            "value": f.value,
                            "confidence": f.confidence,
                        }
                    )
            if host or tech or path_prefix:
                estmt = select(ReconEndpoint).where(ReconEndpoint.task_id == task_id)
                endpoints = session.execute(estmt).scalars().all()
                for ep in endpoints:
                    if host and ep.host != host:
                        continue
                    if path_prefix and not ep.path_normalized.startswith(path_prefix):
                        continue
                    if tech and tech not in (ep.tech_stack or []):
                        continue
                    results.append(
                        {
                            "kind": "endpoint",
                            "host": ep.host,
                            "path": ep.path_normalized,
                            "method": ep.method,
                            "status_code": ep.status_code,
                            "auth_required": ep.auth_required,
                            "tech_stack": ep.tech_stack,
                            "confidence": ep.confidence,
                        }
                    )
            if tech:
                tstmt = select(ReconTechnique).where(ReconTechnique.task_id == task_id)
                techniques = session.execute(tstmt).scalars().all()
                for t in techniques:
                    if tech in t.name or tech in (t.category or ""):
                        results.append(
                            {
                                "kind": "technique",
                                "name": t.name,
                                "status": t.status,
                                "success_count": t.success_count,
                                "confidence": t.confidence,
                            }
                        )
        results.sort(key=lambda r: -r.get("confidence", 0))
        return results[:limit]

    # ------------------------------------------------------------------
    # 分层索引构建（L0/L1/L2）
    # ------------------------------------------------------------------

    def build_index_view(self, task_id: str, objective: str = "") -> str:
        """构建 L0/L1/L2 分层注入块（设计文档 §3.3）。

        - L0 目标基线：从 recon_sessions + 高置信度事实（≥0.8）摘出目标画像。
        - L1 相关端点/技术栈（token 预算 500）：高价值端点 + 技术栈概要。
        - L2 历史成功利用复用（token 预算 1500）：confirmed/exploited 威胁与
          成功 technique。

        确定性裁剪：按 token 预算截断，保证注入不超预算。
        """
        with self._session_factory() as session:
            facts = session.execute(
                select(ReconFact).where(ReconFact.task_id == task_id)
            ).scalars().all()
            endpoints = session.execute(
                select(ReconEndpoint).where(ReconEndpoint.task_id == task_id)
            ).scalars().all()
            techniques = session.execute(
                select(ReconTechnique).where(ReconTechnique.task_id == task_id)
            ).scalars().all()
            threats = session.execute(
                select(ReconThreat).where(ReconThreat.task_id == task_id)
            ).scalars().all()

        sections: List[str] = []

        # ---- L0: 目标基线 ----
        l0_lines = ["## L0 目标基线 (recon)"]
        high_conf = sorted(
            [f for f in facts if f.confidence >= 0.8],
            key=lambda f: -f.confidence,
        )
        for f in high_conf[:15]:
            l0_lines.append(f"- [{f.category}] {f.key}: {f.value}")
        # 站点总览（由 recon_http_transactions 现算，不落物化表）
        overview = self.build_site_overview(task_id)
        if overview["host_count"] or overview["endpoint_count"]:
            l0_lines.append(
                f"- 站点总览: 主机 {overview['host_count']} | 端点观测 {overview['endpoint_count']} | "
                f"需认证 {overview['auth_required_endpoint_count']} | 输入点 {overview['input_point_count']}"
            )
            if overview["status_histogram"]:
                l0_lines.append(f"- 状态码分布: {overview['status_histogram']}")
            if overview["top_technologies"]:
                l0_lines.append(
                    "- 技术栈 Top: "
                    + ", ".join(f"{t}({c})" for t, c in overview["top_technologies"][:6])
                )
        if endpoints:
            hosts = sorted({e.host for e in endpoints if e.host})
            if hosts:
                l0_lines.append(f"- 已知主机: {', '.join(hosts)}")
        sections.append("\n".join(l0_lines))
        logger.info(
            "[RECON-L0] task=%s | facts_total=%d high_conf_shown=%d hosts=%s | injected_lines=%d block_len=%d",
            task_id, len(facts), len(high_conf[:15]),
            sorted({e.host for e in endpoints if e.host}),
            len(l0_lines), len(sections[-1]),
        )

        # ---- L1: 相关端点/技术栈（≤500 tokens）----
        l1_lines = ["## L1 端点与技术栈 (recon)"]
        for ep in sorted(endpoints, key=lambda e: -e.confidence)[:20]:
            line = f"- {ep.method} {ep.path_normalized}"
            if ep.auth_required:
                line += " [auth]"
            if ep.tech_stack:
                line += f" tech={ep.tech_stack}"
            if ep.discovered_via:
                line += f" via={ep.discovered_via}"
            l1_lines.append(line)
        l1_block = self._fit_budget("\n".join(l1_lines), L1_TOKEN_BUDGET)
        if l1_block:
            sections.append(l1_block)
            logger.info(
                "[RECON-L1] task=%s | endpoints_total=%d shown=%d (cap=20) | budget=%d used_tokens=%d injected_lines=%d",
                task_id, len(endpoints), len(l1_lines) - 1,
                L1_TOKEN_BUDGET, num_tokens_from_string(l1_block), len(l1_block.splitlines()),
            )

        # ---- L2: 历史成功利用复用（≤1500 tokens）----
        l2_lines = ["## L2 可复用利用经验 (recon)"]
        confirmed = [
            t for t in threats
            if t.status in (ThreatStatus.confirmed.value, ThreatStatus.exploited.value)
        ]
        for t in confirmed[:10]:
            summary = self.crypto.decrypt(t.evidence_summary) if t.evidence_summary else ""
            label = t.cve_id or t.title
            line = f"- [{t.severity}] {label}"
            if t.affected_endpoint:
                line += f" @ {t.affected_endpoint}"
            if summary:
                line += f" :: {summary[:120]}"
            l2_lines.append(line)
        for t in sorted(techniques, key=lambda x: -x.success_count)[:10]:
            if t.success_count > 0:
                l2_lines.append(f"- 成功手法: {t.name} (×{t.success_count})")
        l2_block = self._fit_budget("\n".join(l2_lines), L2_TOKEN_BUDGET)
        if l2_block:
            sections.append(l2_block)
            logger.info(
                "[RECON-L2] task=%s | threats_total=%d confirmed_shown=%d techniques_success=%d | budget=%d used_tokens=%d injected_lines=%d",
                task_id, len(threats), len(confirmed[:10]),
                sum(1 for t in techniques if t.success_count > 0),
                L2_TOKEN_BUDGET, num_tokens_from_string(l2_block), len(l2_block.splitlines()),
            )

        full = "\n\n".join(sections)
        logger.info(
            "[RECON-INDEX] task=%s | total_sections=%d total_block_len=%d total_tokens=%d",
            task_id, len(sections), len(full), num_tokens_from_string(full),
        )
        return full

    # ------------------------------------------------------------------
    # PG 聚合层同步（第二阶）
    # ------------------------------------------------------------------

    async def upsert_to_pg(
        self,
        target_id: str,
        tenant_id: str,
        task_id: str,
        async_session_factory,
    ) -> None:
        """将本地库按 target_id 维度聚合 upsert 到 PG（recon_facts_agg / recon_threats_agg）。

        收敛策略（INSERT ... ON CONFLICT DO UPDATE）：
        - 冲突时取高置信度；低置信度候选仅追加 source_tasks 与刷新 last_seen。
        - 同一目标多次任务的行数收敛为唯一约束维度（不随任务次数线性增长）。

        Args:
            target_id: 授权目标 UUID（PG 聚合维度）。
            tenant_id: 租户 UUID（多租户隔离）。
            task_id: 来源任务 UUID（追加到 source_tasks 累计）。
            async_session_factory: AsyncSessionLocal 工厂（注入以避免循环导入）。
        """
        from sqlalchemy.dialects.postgresql import insert as pg_insert
        from uuid import UUID as _UUID

        from sqlalchemy import func, update

        from pobi_v2.db.recon_models import (
            ReconEndpointAgg,
            ReconFactAgg,
            ReconHttpTransactionAgg,
            ReconThreatAgg,
            ReconThreatEvidenceLink,
        )

        tgt = _UUID(str(target_id))
        ten = _UUID(str(tenant_id))
        tk = str(task_id)

        # 增量读取：仅取自上次成功同步以来新增/变化的脏行（pg_synced_at IS NULL）。
        with self._session_factory() as session:
            facts = session.execute(
                select(ReconFact).where(
                    ReconFact.task_id == tk, ReconFact.pg_synced_at.is_(None)
                )
            ).scalars().all()
            threats = session.execute(
                select(ReconThreat).where(
                    ReconThreat.task_id == tk, ReconThreat.pg_synced_at.is_(None)
                )
            ).scalars().all()
            endpoints = session.execute(
                select(ReconEndpoint).where(
                    ReconEndpoint.task_id == tk, ReconEndpoint.pg_synced_at.is_(None)
                )
            ).scalars().all()
            tx_rows_raw = session.execute(
                select(ReconHttpTransaction).where(
                    ReconHttpTransaction.task_id == tk,
                    ReconHttpTransaction.pg_synced_at.is_(None),
                )
            ).scalars().all()

        fact_rows = [
            {
                "target_id": tgt,
                "tenant_id": ten,
                "category": f.category,
                "key": f.key,
                "value": f.value,
                "confidence": f.confidence,
                "source_tasks": [tk],
                "sensitivity": f.sensitivity,
                "details_json": f.details_json or {},
            }
            for f in facts
        ]
        threat_rows = [
            {
                "target_id": tgt,
                "tenant_id": ten,
                "cve_id": t.cve_id,
                "title": t.title,
                "category": t.category,
                "severity": t.severity,
                "status": t.status,
                "cvss_score": t.cvss_score,
                "target_endpoint": t.affected_endpoint,
                "evidence_summary": t.evidence_summary,
                "confidence": t.confidence,
                "source_tasks": [tk],
            }
            for t in threats
        ]
        endpoint_rows = [
            {
                "target_id": tgt,
                "tenant_id": ten,
                "host": e.host,
                "path_normalized": e.path_normalized,
                "method": e.method,
                "status_code": e.status_code,
                "auth_required": e.auth_required,
                "tech_stack": e.tech_stack or [],
                "parameters": e.parameters or [],
                "notes": e.notes,
                "discovered_via": e.discovered_via,
                "confidence": e.confidence,
                "source_tasks": [tk],
            }
            for e in endpoints
        ]
        # HTTP 事务：按 (method, url) 折叠，同一命令内不出现重复冲突键
        # （recon_http_transactions 为 append-only 流水，保留多次观测，故脏行可能
        # 含同 (method, url) 多条；PG 唯一键 (target_id, tenant_id, method, url)
        # 在同 INSERT 命令内遇重复即 CardinalityViolation）。折叠取 id 最大（最新）
        # 一行代表，其余脏行一并标记已同步（防漏同步）。
        _tx_fold: Dict[Tuple[str, str], Any] = {}
        tx_synced_ids: List[int] = []
        for x in tx_rows_raw:
            tx_synced_ids.append(x.id)
            fk = (x.method or "", x.url or "")
            cur = _tx_fold.get(fk)
            if cur is None or (x.id or 0) > (cur.id or 0):
                _tx_fold[fk] = x
        tx_rows = [
            {
                "target_id": tgt,
                "tenant_id": ten,
                "host": x.host,
                "path_normalized": x.path_normalized,
                "url": x.url,
                "method": x.method,
                "status_code": x.status_code,
                "source": x.source,
                "response_title": x.response_title,
                "content_type": x.content_type,
                "response_size": x.response_size,
                "response_time_ms": x.response_time_ms,
                "tech_stack": x.tech_stack or [],
                "parameters": x.detected_params or [],
                "auth_used": x.auth_used,
                "auth_required": x.auth_required,
                "storage_strategy": x.storage_strategy,
                "response_body": x.response_body or "",
                "body_compressed": x.body_compressed,
                "source_tasks": [tk],
            }
            for x in _tx_fold.values()
        ]

        async with async_session_factory() as pg:
            if fact_rows:
                stmt = pg_insert(ReconFactAgg).values(fact_rows)
                stmt = stmt.on_conflict_do_update(
                    index_elements=["target_id", "category", "key"],
                    set_={
                        "value": stmt.excluded.value,
                        # 内容始终更新（最新 wins）；confidence 取历史与新的最大值。
                        "confidence": func.greatest(
                            ReconFactAgg.confidence, stmt.excluded.confidence
                        ),
                        "sensitivity": stmt.excluded.sensitivity,
                        "details_json": stmt.excluded.details_json,
                        # 注：source_tasks 列为 JSON 类型，PG 的 json||json 操作符不存在，
                        # 故冲突时取本次写入的任务列表（最新来源），不累计历史任务。
                        # 行收敛（同一事实唯一一行）已满足增量更新需求。
                        "source_tasks": stmt.excluded.source_tasks,
                        "last_seen": _utcnow(),
                    },
                )
                await pg.execute(stmt)
            if threat_rows:
                stmt = pg_insert(ReconThreatAgg).values(threat_rows)
                stmt = stmt.on_conflict_do_update(
                    index_elements=[
                        "target_id",
                        "target_endpoint",
                        "category",
                        "cve_id",
                    ],
                    set_={
                        "title": stmt.excluded.title,
                        "severity": stmt.excluded.severity,
                        "status": stmt.excluded.status,
                        "cvss_score": stmt.excluded.cvss_score,
                        "evidence_summary": stmt.excluded.evidence_summary,
                        "confidence": func.greatest(
                            ReconThreatAgg.confidence, stmt.excluded.confidence
                        ),
                        "source_tasks": stmt.excluded.source_tasks,
                        "last_seen": _utcnow(),
                    },
                )
                await pg.execute(stmt)
            if endpoint_rows:
                stmt = pg_insert(ReconEndpointAgg).values(endpoint_rows)
                stmt = stmt.on_conflict_do_update(
                    index_elements=[
                        "target_id",
                        "host",
                        "path_normalized",
                        "method",
                    ],
                    set_={
                        "status_code": stmt.excluded.status_code,
                        "auth_required": stmt.excluded.auth_required,
                        "tech_stack": stmt.excluded.tech_stack,
                        "parameters": stmt.excluded.parameters,
                        "notes": stmt.excluded.notes,
                        "discovered_via": stmt.excluded.discovered_via,
                        "confidence": func.greatest(
                            ReconEndpointAgg.confidence, stmt.excluded.confidence
                        ),
                        "source_tasks": stmt.excluded.source_tasks,
                        "last_seen": _utcnow(),
                    },
                )
                await pg.execute(stmt)
            if tx_rows:
                def _build_tx_stmt(rows: List[Dict[str, Any]]):
                    s = pg_insert(ReconHttpTransactionAgg).values(rows)
                    return s.on_conflict_do_update(
                        index_elements=["target_id", "tenant_id", "method", "url"],
                        set_={
                            "host": s.excluded.host,
                            "path_normalized": s.excluded.path_normalized,
                            "status_code": s.excluded.status_code,
                            "source": s.excluded.source,
                            "response_title": s.excluded.response_title,
                            "content_type": s.excluded.content_type,
                            "response_size": s.excluded.response_size,
                            "response_time_ms": s.excluded.response_time_ms,
                            "tech_stack": s.excluded.tech_stack,
                            "parameters": s.excluded.parameters,
                            "auth_used": s.excluded.auth_used,
                            "auth_required": s.excluded.auth_required,
                            "storage_strategy": s.excluded.storage_strategy,
                            "response_body": s.excluded.response_body,
                            "body_compressed": s.excluded.body_compressed,
                            "source_tasks": s.excluded.source_tasks,
                            "last_seen": _utcnow(),
                        },
                    )

                try:
                    await pg.execute(_build_tx_stmt(tx_rows))
                except IntegrityError as ie:
                    _orig = getattr(ie, "orig", ie)
                    if "CardinalityViolation" in type(_orig).__name__ or "cannot affect row a second time" in str(ie):
                        # 脏行含重复冲突键（本不应发生：fold 已按 (method,url) 去重；
                        # 此处兜底防御，避免批量 INSERT 同命令重复导致整体失败丢数据）。
                        logger.warning(
                            "[RECON-PG] tx 批量 upsert 遇重复冲突键,降级逐条 upsert (task=%s)", tk
                        )
                        for row in tx_rows:
                            await pg.execute(_build_tx_stmt([row]))
                    else:
                        raise
            await pg.commit()

        # 增量打标：仅 PG 提交成功后，将本次成功同步的行标记为已同步（下次跳过）。
        if fact_rows or threat_rows or endpoint_rows or tx_rows:
            now = _utcnow()
            with self._session_factory() as session:
                if fact_rows:
                    session.execute(
                        update(ReconFact)
                        .where(
                            ReconFact.task_id == tk,
                            ReconFact.id.in_([f.id for f in facts]),
                        )
                        .values(pg_synced_at=now)
                    )
                if threat_rows:
                    session.execute(
                        update(ReconThreat)
                        .where(
                            ReconThreat.task_id == tk,
                            ReconThreat.id.in_([t.id for t in threats]),
                        )
                        .values(pg_synced_at=now)
                    )
                if endpoint_rows:
                    session.execute(
                        update(ReconEndpoint)
                        .where(
                            ReconEndpoint.task_id == tk,
                            ReconEndpoint.id.in_([e.id for e in endpoints]),
                        )
                        .values(pg_synced_at=now)
                    )
                if tx_rows:
                    session.execute(
                        update(ReconHttpTransaction)
                        .where(
                            ReconHttpTransaction.task_id == tk,
                            ReconHttpTransaction.id.in_(tx_synced_ids),
                        )
                        .values(pg_synced_at=now)
                    )
                session.commit()

    async def sync_local_artifacts_to_pg(
        self,
        task_root: Path,
        target_id: str,
        tenant_id: str,
        task_id: str,
        async_session_factory,
    ) -> None:
        """任务终态落库：将本地非认证 artifacts 沉淀到 PG 三张聚合表。

        复用 finally 出口（deadend_runner），在 upsert_to_pg 之后调用一次。
        此时任务已终态，本地文件定型、完整可信。仅 upsert 非认证类产物：
        - task_memory_agg：agent/<agent_id>/<session_id>/memory/summaries/<role>.md
        - task_context_agg：agent/run_context/context.txt
        - task_metrics_agg：metrics/metrics.json + rag/ 索引引用名
        显式跳过 agent/auth_context/*（每次重认证，凭据不入 PG）。

        幂等 upsert（INSERT ... ON CONFLICT DO UPDATE）：冲突取本次列表、
        刷新 last_seen，行收敛为唯一约束维度。文件缺失（如摘要未生成）优雅跳过。

        Args:
            task_root: 任务本地目录（tasks/<task_id>）。
            target_id: 授权目标 UUID（PG 聚合维度）。
            tenant_id: 租户 UUID（多租户隔离）。
            task_id: 来源任务 UUID（追加到 source_tasks）。
            async_session_factory: AsyncSessionLocal 工厂（注入以避免循环导入）。
        """
        from sqlalchemy.dialects.postgresql import insert as pg_insert
        from uuid import UUID as _UUID

        from pobi_v2.db.recon_models import (
            TaskContextAgg,
            TaskMemoryAgg,
            TaskMetricsAgg,
        )

        tgt = _UUID(str(target_id))
        ten = _UUID(str(tenant_id))
        tk = str(task_id)
        root = Path(task_root)
        # 诊断：旧任务沉淀落库前，确认将要聚合的本地文件是否存在（供同步链路排查）。
        _summaries_dir = root / "agent" / "memory" / "summaries"
        _ctx_dest = root / "agent" / "run_context" / "context.txt"
        _mtr_dest = root / "metrics" / "metrics.json"
        _memory_files = sorted(p.name for p in _summaries_dir.glob("*.md")) if _summaries_dir.exists() else []
        logger.info(
            "[SEED-OUT] 旧任务沉淀开始落库 | source_task_id=%s | target_id=%s | "
            "task_root=%s | memory摘要数=%d | context存在=%s | metrics存在=%s",
            tk, str(tgt), str(root), len(_memory_files), _ctx_dest.exists(), _mtr_dest.exists(),
        )
        now = _utcnow()

        # --- 读取本地文件（一次性，IO 可控） ---
        memory_rows: List[Dict[str, Any]] = []
        summaries_dir = root / "agent"
        if summaries_dir.exists():
            # 遍历 agent/<agent_id>/<session_id>/memory/summaries/<role>.md
            for md in summaries_dir.glob("*/memory/summaries/*.md"):
                role = md.stem
                text = md.read_text(encoding="utf-8", errors="replace").strip()
                if not text:
                    continue
                memory_rows.append(
                    {
                        "target_id": tgt,
                        "tenant_id": ten,
                        "agent_role": role,
                        "summary_text": text,
                        "source_tasks": [tk],
                        "sensitivity": "internal",
                        "first_seen": now,
                        "last_seen": now,
                    }
                )

        context_text = ""
        context_file = root / "agent" / "run_context" / "context.txt"
        if context_file.exists():
            context_text = context_file.read_text(
                encoding="utf-8", errors="replace"
            ).strip()

        metrics_json: Dict[str, Any] = {}
        metrics_file = root / "metrics" / "metrics.json"
        if metrics_file.exists():
            try:
                import json

                metrics_json = json.loads(
                    metrics_file.read_text(encoding="utf-8", errors="replace") or "{}"
                )
            except (json.JSONDecodeError, OSError):
                metrics_json = {}

        # rag 索引仅存引用名（不存向量二进制），扫描 rag/ 下 *.db
        rag_index_ref: List[str] = []
        rag_dir = root / "rag"
        if rag_dir.exists():
            rag_index_ref = sorted(
                str(p.relative_to(root)) for p in rag_dir.rglob("*.db")
            )

        async with async_session_factory() as pg:
            if memory_rows:
                stmt = pg_insert(TaskMemoryAgg).values(memory_rows)
                stmt = stmt.on_conflict_do_update(
                    index_elements=["target_id", "agent_role"],
                    set_={
                        "summary_text": stmt.excluded.summary_text,
                        "source_tasks": stmt.excluded.source_tasks,
                        "sensitivity": stmt.excluded.sensitivity,
                        "last_seen": _utcnow(),
                    },
                )
                await pg.execute(stmt)
            if context_text:
                stmt = pg_insert(TaskContextAgg).values(
                    [
                        {
                            "target_id": tgt,
                            "tenant_id": ten,
                            "content_text": context_text,
                            "source_tasks": [tk],
                            "sensitivity": "internal",
                            "first_seen": now,
                            "last_seen": now,
                        }
                    ]
                )
                stmt = stmt.on_conflict_do_update(
                    index_elements=["target_id"],
                    set_={
                        "content_text": stmt.excluded.content_text,
                        "source_tasks": stmt.excluded.source_tasks,
                        "sensitivity": stmt.excluded.sensitivity,
                        "last_seen": _utcnow(),
                    },
                )
                await pg.execute(stmt)
            # metrics 始终 upsert（即使为空 json，也记录该目标已跑过任务）
            stmt = pg_insert(TaskMetricsAgg).values(
                [
                    {
                        "target_id": tgt,
                        "tenant_id": ten,
                        "metrics_json": metrics_json,
                        "rag_index_ref": rag_index_ref,
                        "source_tasks": [tk],
                        "first_seen": now,
                        "last_seen": now,
                    }
                ]
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=["target_id"],
                set_={
                    "metrics_json": stmt.excluded.metrics_json,
                    "rag_index_ref": stmt.excluded.rag_index_ref,
                    "source_tasks": stmt.excluded.source_tasks,
                    "last_seen": _utcnow(),
                },
            )
            await pg.execute(stmt)
            await pg.commit()

    async def seed_local_artifacts(
        self,
        task_root: Path,
        target_id: str,
        tenant_id: str,
        async_session_factory,
    ) -> Dict[str, Any]:
        """新任务启动期复用：按 target_id 从 PG 沉淀层拉取历史，写回本地文件。

        供 ContextEngine 与 _persist_agent_summary 启动即读到历史经验。
        返回摘要 dict（memory/context/metrics 命中情况），上层可用于 prompt 注入。

        Args:
            task_root: 新任务本地目录（tasks/<task_id>），历史沉淀写回此处。
            target_id: 授权目标 UUID（PG 聚合维度）。
            tenant_id: 租户 UUID（多租户隔离）。
            async_session_factory: AsyncSessionLocal 工厂（注入以避免循环导入）。

        Returns:
            dict: {"memory": {role: text}, "context": str, "metrics": dict,
                   "rag_index_ref": list, "seeded": bool}
        """
        from uuid import UUID as _UUID

        from pobi_v2.db.recon_models import (
            TaskContextAgg,
            TaskMemoryAgg,
            TaskMetricsAgg,
        )

        tgt = _UUID(str(target_id))
        ten = _UUID(str(tenant_id))
        result: Dict[str, Any] = {
            "memory": {},
            "context": "",
            "metrics": {},
            "rag_index_ref": [],
            "seeded": False,
        }
        async with async_session_factory() as pg:
            mem_aggs = (
                await pg.execute(
                    select(TaskMemoryAgg).where(
                        TaskMemoryAgg.target_id == tgt,
                        TaskMemoryAgg.tenant_id == ten,
                    )
                )
            ).scalars().all()
            ctx_aggs = (
                await pg.execute(
                    select(TaskContextAgg).where(
                        TaskContextAgg.target_id == tgt,
                        TaskContextAgg.tenant_id == ten,
                    )
                )
            ).scalars().all()
            mtr_aggs = (
                await pg.execute(
                    select(TaskMetricsAgg).where(
                        TaskMetricsAgg.target_id == tgt,
                        TaskMetricsAgg.tenant_id == ten,
                    )
                )
            ).scalars().all()

        if not (mem_aggs or ctx_aggs or mtr_aggs):
            return result

        # 写回本地：memory 摘要 + context.txt（仅当本地尚无该内容时写入，避免覆盖新任务自身产出）
        root = Path(task_root)
        logger.info(
            "[SEED-IN] 命中 PG 历史沉淀：memory=%d / context=%s / metrics=%s | 写回根目录=%s",
            len(mem_aggs), bool(ctx_aggs), bool(mtr_aggs), root,
        )
        summaries_dir = root / "agent" / "memory" / "summaries"
        for m in mem_aggs:
            result["memory"][m.agent_role] = m.summary_text
            dest = summaries_dir / f"{m.agent_role}.md"
            if not dest.exists():
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(m.summary_text, encoding="utf-8")
        if ctx_aggs:
            result["context"] = ctx_aggs[0].content_text
            ctx_dest = root / "agent" / "run_context" / "context.txt"
            if not ctx_dest.exists() and ctx_aggs[0].content_text:
                ctx_dest.parent.mkdir(parents=True, exist_ok=True)
                ctx_dest.write_text(ctx_aggs[0].content_text, encoding="utf-8")
        if mtr_aggs:
            result["metrics"] = mtr_aggs[0].metrics_json or {}
            result["rag_index_ref"] = mtr_aggs[0].rag_index_ref or []
        result["seeded"] = True
        return result

    async def seed_from_pg(
        self,
        target_id: str,
        tenant_id: str,
        async_session_factory,
    ) -> SeedResult:
        """基线续扫预热：按 target_id 从 PG 聚合层拉取历史沉淀，灌入本地库。

        任务启动期调用：把目标历史侦察资产（端点/技术栈/已验证漏洞）写入本地库，
        使 ContextEngine 后续 get_unified_context 自动挂载 L0/L1/L2 基线，跨任务复用。

        增量续扫语义（设计文档 §5.4）：已存在于本地库的同键记录不重复灌入
        （计入 ``already_covered_count``），仅在 PG 置信度更高时覆盖；首次出现的
        记录计入 ``seeded_count``。返回的覆盖清单供上层 prompt 显式跳过重复工作。

        Args:
            target_id: 授权目标 UUID（PG 聚合维度）。
            tenant_id: 租户 UUID（多租户隔离）。
            async_session_factory: AsyncSessionLocal 工厂。

        Returns:
            SeedResult：新灌入计数、已覆盖计数与覆盖清单（端点/技术栈/威胁）。
        """
        from uuid import UUID as _UUID

        from pobi_v2.db.recon_models import (
            ReconEndpointAgg,
            ReconFactAgg,
            ReconHttpTransactionAgg,
            ReconThreatAgg,
        )

        tgt = _UUID(str(target_id))
        ten = _UUID(str(tenant_id))
        async with async_session_factory() as pg:
            endpoint_aggs = (
                await pg.execute(
                    select(ReconEndpointAgg).where(
                        ReconEndpointAgg.target_id == tgt,
                        ReconEndpointAgg.tenant_id == ten,
                    )
                )
            ).scalars().all()
            # 目标级 HTTP 事务骨架（2026-09-02）：新任务 seed 已抓 url，
            # covered_block / L1 增量提示避免重复枚举。
            tx_aggs = (
                await pg.execute(
                    select(ReconHttpTransactionAgg).where(
                        ReconHttpTransactionAgg.target_id == tgt,
                        ReconHttpTransactionAgg.tenant_id == ten,
                    )
                )
            ).scalars().all()
            fact_aggs = (
                await pg.execute(
                    select(ReconFactAgg).where(
                        ReconFactAgg.target_id == tgt,
                        ReconFactAgg.tenant_id == ten,
                    )
                )
            ).scalars().all()
            # 2026-09-10：认证态禁止跨任务复用（架构约定：每次任务重新发起认证）。
            # PG 沉淀中的 authentication 类 fact 多为上一任务的瞬时结论，却带
            # confidence=1.0：实测出现过 "authenticated session available" 但会话目录为空、
            # "全部凭据均无效" 与事实相反。回灌会让新任务带着错误前提开工。
            _AUTH_KEYS_EXCLUDED = {
                "auth_profile",
                "auth_mode",
                "auth_status",
                "authenticator_summary",
                "authenticator_proofs",
            }
            fact_aggs = [
                f
                for f in fact_aggs
                if getattr(f, "category", None) != "authentication"
                and not str(getattr(f, "key", "")).startswith("auth:")
                and str(getattr(f, "key", "")) not in _AUTH_KEYS_EXCLUDED
            ]
            threat_aggs = (
                await pg.execute(
                    select(ReconThreatAgg).where(
                        ReconThreatAgg.target_id == tgt,
                        ReconThreatAgg.tenant_id == ten,
                    )
                )
            ).scalars().all()

        task_id = self._task_id_of_db()
        result = SeedResult()
        # 预载本地已覆盖集合，用于增量裁剪（跳过已覆盖项）。
        with self._session_factory() as session:
            local_endpoints = set(
                session.execute(
                    select(ReconEndpoint.path_normalized).where(
                        ReconEndpoint.task_id == task_id
                    )
                ).scalars()
            )
            local_fact_keys = set(
                session.execute(
                    select(ReconFact.key).where(ReconFact.task_id == task_id)
                ).scalars()
            )
            local_threats = set(
                session.execute(
                    select(ReconThreat.cve_id).where(ReconThreat.task_id == task_id)
                ).scalars()
            )
            # 已覆盖技术栈：聚合本地端点的 tech_stack 并集。
            for ep in session.execute(
                select(ReconEndpoint).where(ReconEndpoint.task_id == task_id)
            ).scalars():
                for t in ep.tech_stack or []:
                    if t not in result.covered_techniques:
                        result.covered_techniques.append(t)
            for th in session.execute(
                select(ReconThreat).where(ReconThreat.task_id == task_id)
            ).scalars():
                if th.status in ("confirmed", "exploited"):
                    result.covered_threats.append(th.cve_id or th.title)

        # 灌入结构化端点资产（recon_endpoints_agg，per-target 收敛，最完整）。
        for ea in endpoint_aggs:
            if ea.path_normalized in local_endpoints:
                result.already_covered_count += 1
            else:
                result.seeded_count += 1
                local_endpoints.add(ea.path_normalized)
            self.upsert_endpoint(
                task_id,
                path_normalized=ea.path_normalized,
                host=ea.host,
                method=ea.method,
                status_code=ea.status_code,
                auth_required=ea.auth_required,
                tech_stack=ea.tech_stack or [],
                parameters=ea.parameters or [],
                notes=ea.notes,
                discovered_via=ea.discovered_via,
                confidence=ea.confidence,
            )
            if ea.path_normalized not in result.covered_endpoints:
                result.covered_endpoints.append(ea.path_normalized)

        # 灌入目标级 HTTP 事务骨架（2026-09-02）：url 级最新状态并入端点树，
        # 供 covered_block / L1 增量提示（同目标新任务不重复枚举已抓 url）。
        for ta in tx_aggs:
            if ta.path_normalized in local_endpoints:
                result.already_covered_count += 1
            else:
                result.seeded_count += 1
                local_endpoints.add(ta.path_normalized)
            self.upsert_endpoint(
                task_id,
                path_normalized=ta.path_normalized,
                host=ta.host,
                method=ta.method,
                status_code=ta.status_code,
                auth_required=ta.auth_required,
                tech_stack=ta.tech_stack or [],
                parameters=ta.parameters or [],
                discovered_via=f"{ta.source}:seed",
                confidence=0.6,
            )
            if ta.path_normalized not in result.covered_endpoints:
                result.covered_endpoints.append(ta.path_normalized)

        # 灌入事实（含端点类 category 映射为端点，便于 L1 挂载）。
        for fa in fact_aggs:
            if fa.category == "endpoint" and fa.key.startswith("/"):
                if fa.key in local_endpoints:
                    result.already_covered_count += 1
                else:
                    result.seeded_count += 1
                    local_endpoints.add(fa.key)
                self.upsert_endpoint(
                    task_id,
                    path_normalized=fa.key,
                    notes=fa.value,
                    confidence=fa.confidence,
                )
            else:
                if fa.key in local_fact_keys:
                    result.already_covered_count += 1
                else:
                    result.seeded_count += 1
                    local_fact_keys.add(fa.key)
                self.upsert_fact(
                    task_id,
                    category=fa.category,
                    key=fa.key,
                    value=fa.value,
                    confidence=fa.confidence,
                    details=fa.details_json or {},
                )
            if fa.category == "endpoint" and fa.key.startswith("/"):
                if fa.key not in result.covered_endpoints:
                    result.covered_endpoints.append(fa.key)
        for ta in threat_aggs:
            if ta.cve_id in local_threats:
                result.already_covered_count += 1
            else:
                result.seeded_count += 1
                local_threats.add(ta.cve_id)
            self.upsert_threat(
                task_id,
                cve_id=ta.cve_id,
                title=ta.title,
                category=ta.category,
                severity=ta.severity,
                status=ta.status,
                cvss_score=ta.cvss_score,
                affected_endpoint=ta.target_endpoint,
                evidence_summary=ta.evidence_summary,
                confidence=ta.confidence,
            )
            if ta.status in ("confirmed", "exploited"):
                ident = ta.cve_id or ta.title
                if ident and ident not in result.covered_threats:
                    result.covered_threats.append(ident)
        return result

    def covered_assets(self, task_id: str) -> SeedResult:
        """只读统计本地库已覆盖资产清单（不触发 PG）。

        供上层 prompt 注入「已覆盖资产（跳过重复工作）」块：端点、技术栈、
        已确认/已利用威胁。seeded_count 恒为 0（仅统计，不灌入）。
        """
        result = SeedResult()
        with self._session_factory() as session:
            for ep in session.execute(
                select(ReconEndpoint).where(ReconEndpoint.task_id == task_id)
            ).scalars():
                if ep.path_normalized and ep.path_normalized not in result.covered_endpoints:
                    result.covered_endpoints.append(ep.path_normalized)
                for t in ep.tech_stack or []:
                    if t not in result.covered_techniques:
                        result.covered_techniques.append(t)
            for th in session.execute(
                select(ReconThreat).where(ReconThreat.task_id == task_id)
            ).scalars():
                if th.status in ("confirmed", "exploited"):
                    ident = th.cve_id or th.title
                    if ident and ident not in result.covered_threats:
                        result.covered_threats.append(ident)
        result.already_covered_count = (
            len(result.covered_endpoints) + len(result.covered_threats)
        )
        return result

    def build_covered_block(
        self, task_id: str, token_budget: int = 1000
    ) -> str:
        """渲染「已覆盖资产（跳过重复工作）」prompt 块。

        无覆盖时返回空字符串（调用方直接忽略）；超出 token 预算时按行裁剪
        （复用 _fit_budget 语义），保证注入不挤压主 prompt。
        """
        cov = self.covered_assets(task_id)
        lines: List[str] = []
        if cov.covered_endpoints:
            lines.append("- 已覆盖端点: " + ", ".join(cov.covered_endpoints[:40]))
        if cov.covered_techniques:
            lines.append("- 已覆盖技术栈: " + ", ".join(cov.covered_techniques[:20]))
        if cov.covered_threats:
            lines.append("- 已确认/已利用威胁: " + ", ".join(cov.covered_threats[:20]))
        if not lines:
            return ""
        header = "## 已覆盖资产（历史任务沉淀，本轮跳过重复工作）"
        block = header + "\n" + "\n".join(lines)
        return self._fit_budget(block, token_budget)

    # ------------------------------------------------------------------
    # 威胁状态机（设计文档 §5.3）
    # ------------------------------------------------------------------

    def update_threat_status(
        self,
        task_id: str,
        cve_id: str,
        new_status: ThreatStatus | str,
        evidence_summary: str = "",
        confidence: float = 0.7,
        affected_endpoint: str = "",
    ) -> bool:
        """状态机单向推进威胁状态：suspected → confirmed → exploited → remediated。

        规则：
        - 降级（如 exploited → confirmed）拒绝，返回 False 并记 warning。
        - 迁移到 exploited 必须携带 evidence_summary，否则抛 ReconStoreError。
        - cve_id 为空时回退 ``(task_id, affected_endpoint, category)`` 定位（对齐
          PG 聚合键语义：target_id + target_endpoint + category + cve_id）。
        - 目标威胁不存在时抛 ReconStoreError（调用方应先 upsert_threat）。

        Returns:
            True 表示状态已推进；False 表示目标已处于更高状态（幂等跳过）。
        """
        new_st = new_status.value if isinstance(new_status, ThreatStatus) else str(new_status)
        if new_st not in _THREAT_STATUS_ORDER:
            raise ReconStoreError(f"非法威胁状态: {new_st}")
        if new_st == ThreatStatus.exploited.value and not evidence_summary:
            raise ReconStoreError("迁移到 exploited 必须携带 evidence_summary")
        with self._session_factory() as session:
            stmt = select(ReconThreat).where(ReconThreat.task_id == task_id)
            if cve_id:
                stmt = stmt.where(ReconThreat.cve_id == cve_id)
            else:
                # cve_id 为空：回退端点 + 状态非 remediated 的候选（取最高置信度）。
                stmt = stmt.where(
                    ReconThreat.affected_endpoint == affected_endpoint
                ).order_by(ReconThreat.confidence.desc())
            existing = session.execute(stmt.limit(1)).scalars().first()
            if existing is None:
                raise ReconStoreError(
                    f"威胁不存在，无法推进状态: task={task_id} cve={cve_id} ep={affected_endpoint}"
                )
            cur = _THREAT_STATUS_ORDER.get(existing.status, 0)
            new_idx = _THREAT_STATUS_ORDER[new_st]
            if new_idx < cur:
                logger.warning(
                    "RECON 威胁状态降级被拒绝: %s (%s -> %s)",
                    cve_id or affected_endpoint, existing.status, new_st,
                )
                return False
            if new_idx == cur and not (new_st == "exploited" and existing.evidence_summary):
                # 同状态重复提交：幂等返回，仅在缺 evidence 时补充。
                if new_st == "exploited" and not existing.evidence_summary:
                    existing.evidence_summary = self.crypto.encrypt(evidence_summary)
                session.commit()
                return False
            existing.status = new_st
            if evidence_summary:
                existing.evidence_summary = self.crypto.encrypt(evidence_summary)
            existing.confidence = max(existing.confidence, confidence)
            existing.updated_at = _utcnow()
            session.commit()
        return True

    async def reconcile_pg_to_local(
        self,
        target_id: str,
        tenant_id: str,
        async_session_factory,
    ) -> Dict[str, Any]:
        """PG 权威状态反向对账：将 PG 聚合层更高状态的威胁回灌本地库。

        与 seed_from_pg 合并语义（避免二次全量拉取）：本地状态落后于 PG
        （按状态机顺序比较）时覆盖；本地领先则保留本地；本地无此威胁则灌入。
        返回对账摘要：{seeded, upgraded, kept_local, total}。
        """
        from uuid import UUID as _UUID

        from pobi_v2.db.recon_models import ReconThreatAgg

        tgt = _UUID(str(target_id))
        ten = _UUID(str(tenant_id))
        summary: Dict[str, Any] = {"seeded": 0, "upgraded": 0, "kept_local": 0, "total": 0}
        async with async_session_factory() as pg:
            threat_aggs = (
                await pg.execute(
                    select(ReconThreatAgg).where(
                        ReconThreatAgg.target_id == tgt,
                        ReconThreatAgg.tenant_id == ten,
                    )
                )
            ).scalars().all()

        task_id = self._task_id_of_db()
        summary["total"] = len(threat_aggs)
        for ta in threat_aggs:
            with self._session_factory() as session:
                existing = session.execute(
                    select(ReconThreat).where(
                        ReconThreat.task_id == task_id,
                        ReconThreat.cve_id == ta.cve_id,
                    )
                ).scalar_one_or_none()
                if existing is None:
                    self.upsert_threat(
                        task_id,
                        cve_id=ta.cve_id,
                        title=ta.title,
                        category=ta.category,
                        severity=ta.severity,
                        status=ta.status,
                        cvss_score=ta.cvss_score,
                        affected_endpoint=ta.target_endpoint,
                        evidence_summary=ta.evidence_summary,
                        confidence=ta.confidence,
                    )
                    summary["seeded"] += 1
                    continue
                cur_idx = _THREAT_STATUS_ORDER.get(existing.status, 0)
                pg_idx = _THREAT_STATUS_ORDER.get(ta.status, 0)
                if pg_idx > cur_idx:
                    existing.status = ta.status
                    if ta.evidence_summary:
                        existing.evidence_summary = self.crypto.encrypt(ta.evidence_summary)
                    existing.confidence = max(existing.confidence, ta.confidence)
                    existing.updated_at = _utcnow()
                    session.commit()
                    summary["upgraded"] += 1
                else:
                    summary["kept_local"] += 1
        return summary

    def _task_id_of_db(self) -> str:
        """从 db 文件名反推 task_id（用于 PG 同步时定位本地库归属）。"""
        return self.db_path.stem

    def _sync_fts(self, fact_id: int, key: str, value: str) -> None:
        """将事实同步进 FTS5 外部内容索引（新增/更新均替换行）。"""
        try:
            with self._engine.connect() as conn:
                conn.exec_driver_sql(
                    "INSERT OR REPLACE INTO recon_facts_fts(rowid, key, value) "
                    "VALUES (?, ?, ?)",
                    (fact_id, key, value),
                )
                conn.commit()
        except Exception as exc:  # noqa: BLE001 - FTS 同步失败不阻断主流程
            logger.warning("RECON FTS5 同步失败（已忽略）: %s", exc)

    @staticmethod
    def _fts_query(keyword: str) -> str:
        """将用户关键词转义为 FTS5 安全短语查询（双引号包裹，转义内部引号）。"""
        escaped = keyword.replace('"', '""')
        return f'"{escaped}"'

    # ------------------------------------------------------------------
    # 端点派生 / 站点总览 / 基线块（recon_http_transactions 为单一真源）
    # ------------------------------------------------------------------

    async def derive_endpoints_from_transactions(
        self, task_id: str, session_id: Optional[int] = None, db=None
    ) -> List[Dict[str, Any]]:
        """从 recon_http_transactions 按 (host, path_normalized) 派生端点树。

        设计：recon_http_transactions 为单一真源（append-only 流水，保留多次观测，
        如 katana 匿名 403 vs requester 带 cookie 200 的认证差异）。端点树不再由
        sitemap/requester 双写，统一在此由 tx 派生并写回 recon_endpoints，消除双写
        不一致，并使 PG 同步端点的唯一键天然收敛。

        派生规则（与本地 recon_endpoints 幂等键 (task_id, path_normalized) 对齐，
        方法视为路径维度下的属性而非独立标识）：
        - GROUP BY (host, path_normalized)
        - status_code：2xx > 3xx > 4xx > 5xx，同档取最新观测
        - method：取最新观测（多方法在 notes 标注）
        - detected_params / tech_stack：取并集
        - auth_required：任一观测为真即标记
        - discovered_via：取首次来源
        - request_count：记该路径总观测数
        - is_endpoint：默认 True（供 L1/L2 消费）
        """
        target_db = db or self._session_factory()
        owns = db is None
        try:
            rows = (
                target_db.query(ReconHttpTransaction)
                .filter(ReconHttpTransaction.task_id == task_id)
                .all()
            )
            groups: Dict[Tuple[str, str], Dict[str, Any]] = {}
            for tx in rows:
                key = (tx.host or "", tx.path_normalized or "")
                g = groups.get(key)
                if g is None:
                    g = {
                        "host": tx.host,
                        "path_normalized": tx.path_normalized,
                        "method": tx.method,
                        "url": tx.url,
                        "status_code": tx.status_code,
                        "discovered_via": tx.source,
                        "tech_stack": set(),
                        "parameters": set(),
                        "auth_required": bool(tx.auth_required),
                        "request_count": 0,
                        "first_seen": tx.created_at,
                        "last_seen": tx.created_at,
                        "observed_statuses": set(),
                        "observed_status_sources": {},
                        "observed_methods": set(),
                    }
                    groups[key] = g
                g["request_count"] += 1
                g["observed_methods"].add(tx.method or "")
                cur = g["status_code"]
                new = tx.status_code
                if new is None:
                    pass
                elif cur is None or _status_rank(new) > _status_rank(cur):
                    g["status_code"] = new
                g["observed_statuses"].add(str(tx.status_code))
                g["observed_status_sources"].setdefault(str(tx.status_code), tx.source)
                if tx.tech_stack:
                    g["tech_stack"].update(tx.tech_stack)
                if tx.detected_params:
                    g["parameters"].update(tx.detected_params)
                if tx.auth_required:
                    g["auth_required"] = True
                if tx.method:
                    g["method"] = tx.method
                if tx.created_at and (g["first_seen"] is None or tx.created_at < g["first_seen"]):
                    g["first_seen"] = tx.created_at
                if tx.created_at and (g["last_seen"] is None or tx.created_at > g["last_seen"]):
                    g["last_seen"] = tx.created_at

            for key, g in groups.items():
                tech_stack = sorted(g["tech_stack"])
                parameters = sorted(g["parameters"])
                extras = []
                if len(g["observed_statuses"]) > 1:
                    extras.append("多观测状态码: " + ", ".join(sorted(g["observed_statuses"])))
                if len(g["observed_methods"]) > 1:
                    extras.append("方法: " + ",".join(sorted(m for m in g["observed_methods"] if m)))
                notes = "; ".join(extras)
                existing = (
                    target_db.query(ReconEndpoint)
                    .filter(
                        ReconEndpoint.task_id == task_id,
                        ReconEndpoint.path_normalized == g["path_normalized"],
                    )
                    .first()
                )
                if existing:
                    existing.method = g["method"]
                    existing.host = g["host"]
                    existing.url = g["url"]
                    existing.status_code = g["status_code"]
                    existing.discovered_via = g["discovered_via"]
                    existing.tech_stack = tech_stack
                    existing.parameters = parameters
                    existing.auth_required = g["auth_required"]
                    existing.notes = notes
                else:
                    target_db.add(
                        ReconEndpoint(
                            task_id=task_id,
                            session_id=session_id if session_id is not None else _latest_session_id(target_db, task_id),
                            host=g["host"],
                            path_normalized=g["path_normalized"],
                            method=g["method"],
                            status_code=g["status_code"],
                            discovered_via=g["discovered_via"],
                            tech_stack=tech_stack,
                            parameters=parameters,
                            auth_required=g["auth_required"],
                            notes=notes,
                        )
                    )
            target_db.commit()
            return list(groups.values())
        finally:
            if owns:
                target_db.close()

    def build_site_overview(self, task_id: str) -> Dict[str, Any]:
        """从 recon_http_transactions 现算站点总览统计（不落物化表）。

        由 supervisor 启动基线块与 build_index_view 的 L0/L1 消费。
        """
        with self._session_factory() as session:
            rows = (
                session.query(ReconHttpTransaction)
                .filter(ReconHttpTransaction.task_id == task_id)
                .all()
            )
            hosts: set = set()
            status_hist: Dict[int, int] = {}
            auth_endpoints: set = set()
            param_counter: Counter = Counter()
            tech_counter: Counter = Counter()
            form_endpoints: set = set()
            first_seen = last_seen = None
            for tx in rows:
                if tx.host:
                    hosts.add(tx.host)
                if tx.status_code is not None:
                    status_hist[tx.status_code] = status_hist.get(tx.status_code, 0) + 1
                if tx.auth_required:
                    auth_endpoints.add((tx.host, tx.path_normalized, tx.method))
                for p in (tx.detected_params or []):
                    param_counter[p] += 1
                for t in (tx.tech_stack or []):
                    tech_counter[t] += 1
                if tx.detected_params:
                    form_endpoints.add((tx.host, tx.path_normalized, tx.method))
                if tx.created_at:
                    if first_seen is None or tx.created_at < first_seen:
                        first_seen = tx.created_at
                    if last_seen is None or tx.created_at > last_seen:
                        last_seen = tx.created_at
        return {
            "hosts": sorted(hosts),
            "host_count": len(hosts),
            "endpoint_count": len(rows),
            "status_histogram": dict(sorted(status_hist.items())),
            "auth_required_endpoint_count": len(auth_endpoints),
            "top_parameters": param_counter.most_common(10),
            "top_technologies": tech_counter.most_common(10),
            "input_point_count": len(form_endpoints),
            "first_seen_at": first_seen,
            "last_seen_at": last_seen,
        }

    def build_baseline_block(self, task_id: str, token_budget: int = 2000) -> str:
        """组合指纹/站点总览/端点摘要为 supervisor 启动注入块（读时现算）。"""
        overview = self.build_site_overview(task_id)
        with self._session_factory() as session:
            endpoints = (
                session.query(ReconEndpoint)
                .filter(ReconEndpoint.task_id == task_id)
                .order_by(ReconEndpoint.auth_required.desc(), ReconEndpoint.path_normalized)
                .all()
            )
            facts = (
                session.query(ReconFact)
                .filter(ReconFact.task_id == task_id)
                .all()
            )
            techs = (
                session.query(ReconTechnique)
                .filter(ReconTechnique.task_id == task_id)
                .all()
            )
        lines: List[str] = ["## 目标侦察基线（来自历史/前置侦查）"]
        lines.append(
            f"- 主机数: {overview['host_count']} | 端点观测数: {overview['endpoint_count']} | "
            f"需认证端点: {overview['auth_required_endpoint_count']} | 输入点: {overview['input_point_count']}"
        )
        if overview["hosts"]:
            lines.append(f"- 主机: {', '.join(overview['hosts'])}")
        if overview["status_histogram"]:
            lines.append(f"- 状态码分布: {overview['status_histogram']}")
        if overview["top_technologies"]:
            lines.append(
                "- 技术栈 Top: "
                + ", ".join(f"{t}({c})" for t, c in overview["top_technologies"][:8])
            )
        if overview["top_parameters"]:
            lines.append(
                "- 参数名 Top: "
                + ", ".join(f"{p}({c})" for p, c in overview["top_parameters"][:10])
            )
        if facts:
            lines.append("- 已知事实:")
            for f in facts[:12]:
                lines.append(f"  - [{f.category}] {f.key}: {f.value}")
        if techs:
            lines.append("- 已尝试手法:")
            for t in techs[:12]:
                lines.append(f"  - {t.name} ({t.status})")
        if endpoints:
            lines.append("- 关键端点（需认证优先）:")
            for ep in endpoints[:20]:
                flag = " [需认证]" if ep.auth_required else ""
                tech = f" tech={','.join(ep.tech_stack[:3])}" if ep.tech_stack else ""
                lines.append(
                    f"  - {ep.method} {ep.path_normalized} -> {ep.status_code}{flag}{tech}"
                )
        else:
            lines.append("- 暂无端点（历史目标，新任务将从零侦察）")
        return self._fit_budget("\n".join(lines), token_budget)

    def build_pre_recon_json(self, task_id: str) -> Dict[str, Any]:
        """将前置侦查产物组装为结构化 JSON（指纹/WAF/端点综述），供 supervisor 启动注入。

        与 build_baseline_block（自然语言文本块）互补：本方法产出机器可读 JSON，
        全部字段来自 recon 本地库（只读现算），无 LLM 参与、无凭空推断。
        失败时返回空结构，不阻断主流程。
        """
        result: Dict[str, Any] = {"pre_recon": {}}
        try:
            # ---- 1) 目标基础 + 指纹 + WAF（recon_fingerprints 幂等键 task_id+target_url）----
            target: Dict[str, Any] = {}
            fingerprint: Dict[str, Any] = {}
            waf: Dict[str, Any] = {"detected": False, "names": []}
            with self._session_factory() as session:
                row = session.execute(
                    select(ReconFingerprint)
                    .where(ReconFingerprint.task_id == task_id)
                    .order_by(ReconFingerprint.updated_at.desc())
                    .limit(1)
                ).scalar_one_or_none()
                if row is not None:
                    target = {
                        "url": row.target_url,
                        "host": row.host,
                        "status_code": row.status_code,
                    }
                    fp = row.fingerprint_json or {}
                    fingerprint = {
                        "server": fp.get("server") or [],
                        "backend": fp.get("backend") or [],
                        "frontend": fp.get("frontend") or [],
                        "cms": fp.get("cms") or [],
                        "favicon_hash": row.favicon_hash,
                        "matched_rules": row.matched_count,
                        "total_rules": row.rule_count,
                    }
                    waf_list = fp.get("waf") or []
                    if isinstance(waf_list, list) and waf_list:
                        waf = {
                            "detected": True,
                            "names": [
                                w.get("name")
                                for w in waf_list
                                if isinstance(w, dict) and w.get("name")
                            ],
                        }

            # ---- 2) 站点端点综述（recon_endpoints + recon_http_transactions）----
            site_overview = self._build_site_overview_json(task_id)

            # ---- 3) 历史威胁建模结论（recon_threats 已确认/可疑清单）----
            # 来自 seed_from_pg 跨任务灌入或本任务已建模威胁。注入 supervisor 后
            # 引导「先验证历史漏洞是否仍存在/已修复，再补充新发现」，避免重复全量威胁建模。
            threats: list[Dict[str, Any]] = []
            with self._session_factory() as session:
                th_rows = (
                    session.query(ReconThreat)
                    .filter(ReconThreat.task_id == task_id)
                    .order_by(ReconThreat.updated_at.desc())
                    .limit(30)
                    .all()
                )
                for th in th_rows:
                    if th.status in ("confirmed", "exploited", "suspected"):
                        threats.append(
                            {
                                "title": th.title,
                                "category": th.category,
                                "severity": th.severity,
                                "status": th.status,
                                "confidence": th.confidence,
                                "affected_endpoint": th.affected_endpoint or "",
                                "evidence": (th.evidence_summary or "")[:200],
                            }
                        )

            result["pre_recon"] = {
                "target": target,
                "fingerprint": fingerprint,
                "waf": waf,
                "site_overview": site_overview,
                "historical_threats": threats,
            }
        except Exception as exc:  # noqa: BLE001
            logger.warning("RECON 组装 pre_recon JSON 失败（返回空结构）: %s", exc)
        return result

    def _build_site_overview_json(self, task_id: str) -> Dict[str, Any]:
        """从 recon_endpoints / recon_http_transactions 现算端点综述（build_pre_recon_json 辅助）。"""
        overview: Dict[str, Any] = {}
        try:
            with self._session_factory() as session:
                endpoints = (
                    session.query(ReconEndpoint)
                    .filter(ReconEndpoint.task_id == task_id)
                    .all()
                )
                txs = (
                    session.query(ReconHttpTransaction)
                    .filter(ReconHttpTransaction.task_id == task_id)
                    .all()
                )
            methods: Dict[str, int] = {}
            status_dist: Dict[int, int] = {}
            total = len(endpoints)
            with_params = 0
            auth_required = 0
            static_assets = 0
            api_eps = 0
            for ep in endpoints:
                methods[ep.method] = methods.get(ep.method, 0) + 1
                if ep.status_code is not None:
                    status_dist[ep.status_code] = status_dist.get(ep.status_code, 0) + 1
                if ep.parameters:
                    with_params += 1
                if ep.auth_required:
                    auth_required += 1
                path = (ep.path_normalized or "").lower()
                if _looks_like_static_asset(path):
                    static_assets += 1
                if _looks_like_api_path(path):
                    api_eps += 1

            param_counter: Counter = Counter()
            hosts: set = set()
            for tx in txs:
                if tx.host:
                    hosts.add(tx.host)
                for p in (tx.detected_params or []):
                    param_counter[p] += 1

            overview = {
                "total_endpoints": total,
                "api_endpoints": api_eps,
                "endpoints_with_parameters": with_params,
                "endpoints_without_parameters": total - with_params,
                "auth_required_endpoints": auth_required,
                "static_asset_endpoints": static_assets,
                "application_endpoints": total - static_assets,
                "total_http_transactions": len(txs),
                "host_count": len(hosts),
                "methods": dict(sorted(methods.items())),
                "status_code_distribution": dict(sorted(status_dist.items())),
                "top_parameters": [
                    {"name": name, "count": cnt}
                    for name, cnt in param_counter.most_common(10)
                ],
            }
        except Exception as exc:  # noqa: BLE001
            logger.warning("RECON 组装站点综述失败（返回空）: %s", exc)
        return overview

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------

    def _fit_budget(self, text: str, token_budget: int) -> str:
        """按 token 预算确定性截断（保留块首，截断尾部）。"""
        if num_tokens_from_string(text) <= token_budget:
            return text
        lines = text.split("\n")
        out: List[str] = []
        used = 0
        for line in lines:
            line_tokens = num_tokens_from_string(line)
            if used + line_tokens > token_budget:
                break
            out.append(line)
            used += line_tokens
        return "\n".join(out)

    def _write_blob(self, task_id: str, key: str, value: str) -> str:
        """大文本外置 blobs/，返回 blob_ref（相对 recon 目录）。

        写入失败不阻断主流程，返回空字符串由调用方保留摘要。
        """
        try:
            blob_dir = self.db_path.parent / "blobs"
            blob_dir.mkdir(parents=True, exist_ok=True)
            safe_key = re.sub(r"[^A-Za-z0-9_.-]", "_", key)[:64]
            blob_path = blob_dir / f"{task_id}_{safe_key}.blob"
            blob_path.write_text(value, encoding="utf-8")
            _restrict_file_perms(blob_path)
            return f"blobs/{blob_path.name}"
        except Exception as exc:  # noqa: BLE001
            logger.warning("RECON blob 外置失败: %s", exc)
            return ""

    # ------------------------------------------------------------------
    # 只读查询面（供前端/API 结构化展示，纯 dict，evidence 自动解密）
    # ------------------------------------------------------------------

    def get_summary(self, task_id: str) -> Dict[str, object]:
        """聚合单任务侦察总览（计数 + 威胁状态分布 + 阶段进度基线）。

        本地库为空或缺失时返回全 0 计数结构，不抛异常（旁路容错）。
        """
        summary: Dict[str, object] = {
            "assets": {"hosts": 0, "services": 0},
            "facts_count": 0,
            "endpoints_count": 0,
            "techniques_count": 0,
            "threats_count": 0,
            "threats_by_status": {
                "suspected": 0,
                "confirmed": 0,
                "exploited": 0,
                "remediated": 0,
            },
            "threats_by_severity": {
                "critical": 0,
                "high": 0,
                "medium": 0,
                "low": 0,
                "info": 0,
            },
        }
        try:
            with self._session_factory() as session:
                endpoints = session.execute(
                    select(ReconEndpoint).where(ReconEndpoint.task_id == task_id)
                ).scalars().all()
                facts = session.execute(
                    select(ReconFact).where(ReconFact.task_id == task_id)
                ).scalars().all()
                techniques = session.execute(
                    select(ReconTechnique).where(ReconTechnique.task_id == task_id)
                ).scalars().all()
                threats = session.execute(
                    select(ReconThreat).where(ReconThreat.task_id == task_id)
                ).scalars().all()

                hosts = {e.host for e in endpoints if e.host}
                summary["assets"] = {
                    "hosts": len(hosts),
                    "services": len(endpoints),
                }
                summary["facts_count"] = len(facts)
                summary["endpoints_count"] = len(endpoints)
                summary["techniques_count"] = len(techniques)
                summary["threats_count"] = len(threats)
                by_status = summary["threats_by_status"]  # type: ignore[assignment]
                by_sev = summary["threats_by_severity"]  # type: ignore[assignment]
                for th in threats:
                    s = str(th.status) if th.status else "suspected"
                    if s in by_status:
                        by_status[s] += 1  # type: ignore[index]
                    sev = str(th.severity) if th.severity else "info"
                    if sev in by_sev:
                        by_sev[sev] += 1  # type: ignore[index]
        except Exception as exc:  # noqa: BLE001 - 本地库缺失/损坏不阻断
            logger.warning("RECON 读取总览失败（返回空结构）: %s", exc)
        return summary

    def list_facts(
        self, task_id: str, category: Optional[str] = None, limit: int = 200
    ) -> List[Dict[str, object]]:
        """返回事实列表，可选按类目过滤（category 已 normalize）。"""
        out: List[Dict[str, object]] = []
        try:
            with self._session_factory() as session:
                stmt = select(ReconFact).where(ReconFact.task_id == task_id)
                if category:
                    stmt = stmt.where(
                        ReconFact.category == normalize_category(category).value
                    )
                stmt = stmt.order_by(ReconFact.confidence.desc()).limit(limit)
                for f in session.execute(stmt).scalars().all():
                    out.append(
                        {
                            "id": f.id,
                            "category": f.category,
                            "key": f.key,
                            "value": f.value,
                            "confidence": f.confidence,
                            "source": f.source,
                            "created_at": _iso(f.created_at),
                            "updated_at": _iso(f.updated_at),
                        }
                    )
        except Exception as exc:  # noqa: BLE001
            logger.warning("RECON 读取事实失败（返回空列表）: %s", exc)
        return out

    def list_endpoints(
        self, task_id: str, limit: int = 500
    ) -> List[Dict[str, object]]:
        """返回攻击面终端明细（host/path/method/状态码/鉴权/技术栈）。"""
        out: List[Dict[str, object]] = []
        try:
            with self._session_factory() as session:
                stmt = (
                    select(ReconEndpoint)
                    .where(ReconEndpoint.task_id == task_id)
                    .order_by(ReconEndpoint.confidence.desc())
                    .limit(limit)
                )
                for e in session.execute(stmt).scalars().all():
                    out.append(
                        {
                            "id": e.id,
                            "host": e.host,
                            "path": e.path_normalized,
                            "method": e.method,
                            "status_code": e.status_code,
                            "auth_required": e.auth_required,
                            "tech_stack": list(e.tech_stack or []),
                            "parameters": list(e.parameters or []),
                            "discovered_via": e.discovered_via,
                            "confidence": e.confidence,
                        }
                    )
        except Exception as exc:  # noqa: BLE001
            logger.warning("RECON 读取终端失败（返回空列表）: %s", exc)
        return out

    def list_techniques(
        self, task_id: str, limit: int = 500
    ) -> List[Dict[str, object]]:
        """返回已测技术/尝试足迹（recon_techniques），供主控证据驱动收敛。"""
        out: List[Dict[str, object]] = []
        try:
            with self._session_factory() as session:
                stmt = (
                    select(ReconTechnique)
                    .where(ReconTechnique.task_id == task_id)
                    .order_by(ReconTechnique.updated_at.desc())
                    .limit(limit)
                )
                for t in session.execute(stmt).scalars().all():
                    out.append(
                        {
                            "id": t.id,
                            "name": t.name,
                            "category": t.category,
                            "status": t.status,
                            "success_count": t.success_count,
                            "tested_count": t.tested_count,
                            "last_result": t.last_result,
                            "confidence": t.confidence,
                            "updated_at": _iso(t.updated_at),
                        }
                    )
        except Exception as exc:  # noqa: BLE001
            logger.warning("RECON 读取足迹失败（返回空列表）: %s", exc)
        return out

    # ------------------------------------------------------------------
    # 指纹识别明细（recon_fingerprints，前置侦查专用）
    # ------------------------------------------------------------------

    def upsert_fingerprint(
        self,
        task_id: str,
        target_url: str,
        *,
        host: str = "",
        status_code: Optional[int] = None,
        favicon_hash: Optional[int] = None,
        auth_mode: str = "external_only",
        fingerprint: Optional[Dict[str, Any]] = None,
        matched_count: int = 0,
        rule_count: int = 0,
        source: str = "pre_recon",
        session_id: Optional[int] = None,
    ) -> None:
        """幂等写入 recon_fingerprints，冲突键 (task_id, target_url)。

        同 host 二次扫描仅更新时间与结果，不产生重复行。
        """
        if session_id is None:
            session_id = self.ensure_session(task_id)
        with self._session_factory() as session:
            existing = session.execute(
                select(ReconFingerprint).where(
                    ReconFingerprint.task_id == task_id,
                    ReconFingerprint.target_url == target_url,
                )
            ).scalar_one_or_none()
            if existing:
                existing.pg_synced_at = None
                existing.host = host or existing.host
                if status_code is not None:
                    existing.status_code = status_code
                if favicon_hash is not None:
                    existing.favicon_hash = favicon_hash
                existing.auth_mode = auth_mode or existing.auth_mode
                existing.fingerprint_json = fingerprint or existing.fingerprint_json
                existing.matched_count = matched_count
                existing.rule_count = rule_count
                existing.source = source or existing.source
                existing.updated_at = _utcnow()
            else:
                session.add(
                    ReconFingerprint(
                        session_id=session_id,
                        task_id=task_id,
                        target_url=target_url,
                        host=host,
                        status_code=status_code,
                        favicon_hash=favicon_hash,
                        auth_mode=auth_mode,
                        fingerprint_json=fingerprint or {},
                        matched_count=matched_count,
                        rule_count=rule_count,
                        source=source,
                    )
                )
            session.commit()

    def insert_http_transaction(
        self,
        task_id: str,
        *,
        host: str = "",
        path_normalized: str = "",
        url: str = "",
        method: str = "GET",
        status_code: Optional[int] = None,
        source: str = "sitemap:katana",
        request_headers: Optional[Dict[str, Any]] = None,
        request_body: str = "",
        response_headers: Optional[Dict[str, Any]] = None,
        response_body: str = "",
        response_title: str = "",
        content_type: str = "",
        response_size: int = 0,
        response_time_ms: Optional[int] = None,
        tech_stack: Optional[List[str]] = None,
        detected_params: Optional[List[str]] = None,
        detected_forms: Optional[List[Dict[str, Any]]] = None,
        auth_used: bool = False,
        auth_required: bool = False,
        session_id: Optional[int] = None,
    ) -> None:
        """追加一条 HTTP 请求/响应事务（流水表，无幂等键，保留历史可对比）。

        响应体分层存储（2026-09-02）：
        - <100KB → full：明文全量落库 response_body；
        - 100KB–1MB（非 API）→ compressed：gzip 压缩存 body_compressed，
          response_body 仅存 ``_PREVIEW_BYTES`` 字符预览；
        - >1MB（非 API）→ digest：仅存前 ``_DIGEST_PREFIX_BYTES`` 字符摘要；
        - json/xml API 响应例外：即使大也全量保留（≥100KB 走 compressed 压缩存储）。
        完整内容经 ``get_transaction_body`` 按需解压/取全文。
        """
        if session_id is None:
            session_id = self.ensure_session(task_id)
        stored_body = response_body or ""
        strategy = "full"
        compressed: Optional[bytes] = None
        if stored_body:
            size = len(stored_body.encode("utf-8"))
            is_api = bool(_API_CONTENT_TYPE_RE.search((content_type or "")))
            if is_api:
                # API 响应全量保留：≥100KB 压缩存 BLOB，<100KB 明文。
                if size >= _BLOB_THRESHOLD_BYTES:
                    strategy = "compressed"
                    compressed = gzip.compress(stored_body.encode("utf-8"))
                    stored_body = stored_body[:_PREVIEW_BYTES]
            elif size >= _BLOB_THRESHOLD_BYTES:
                if size <= _COMPRESS_MAX_BYTES:
                    strategy = "compressed"
                    compressed = gzip.compress(stored_body.encode("utf-8"))
                    stored_body = stored_body[:_PREVIEW_BYTES]
                else:
                    strategy = "digest"
                    stored_body = stored_body[:_DIGEST_PREFIX_BYTES]
        try:
            with self._session_factory() as session:
                session.add(
                    ReconHttpTransaction(
                        session_id=session_id,
                        task_id=task_id,
                        host=host,
                        path_normalized=path_normalized,
                        url=url,
                        method=method,
                        status_code=status_code,
                        source=source,
                        request_headers=request_headers or {},
                        request_body=request_body or "",
                        response_headers=response_headers or {},
                        response_body=stored_body,
                        storage_strategy=strategy,
                        body_compressed=compressed,
                        response_title=response_title,
                        content_type=content_type,
                        response_size=int(response_size or 0),
                        response_time_ms=response_time_ms,
                        tech_stack=tech_stack or [],
                        detected_params=detected_params or [],
                        detected_forms=detected_forms or [],
                        auth_used=auth_used,
                        auth_required=auth_required,
                    )
                )
                session.commit()
        except Exception as exc:  # noqa: BLE001 - 事务写入失败不阻断主流程
            logger.warning("RECON 事务写入失败（已忽略）: %s", exc)

    def list_http_transactions(
        self,
        task_id: str,
        host: Optional[str] = None,
        path_normalized: Optional[str] = None,
        source: Optional[str] = None,
        limit: int = 500,
    ) -> List[Dict[str, object]]:
        """返回 HTTP 事务流水（站点地图按端点聚合查询的读面）。

        骨架视图：不拖大 body（>100KB 均为 compressed/digest，response_body 仅含
        预览/摘要），完整内容由站点地图经 ``get_transaction_body`` 按需取全文。
        """
        out: List[Dict[str, object]] = []
        try:
            with self._session_factory() as session:
                stmt = select(ReconHttpTransaction).where(
                    ReconHttpTransaction.task_id == task_id
                )
                if host:
                    stmt = stmt.where(ReconHttpTransaction.host == host)
                if path_normalized:
                    stmt = stmt.where(
                        ReconHttpTransaction.path_normalized == path_normalized
                    )
                if source:
                    stmt = stmt.where(ReconHttpTransaction.source == source)
                stmt = stmt.order_by(ReconHttpTransaction.created_at.asc()).limit(limit)
                for t in session.execute(stmt).scalars().all():
                    out.append(self._http_tx_to_dict(t))
        except Exception as exc:  # noqa: BLE001
            logger.warning("RECON 读取事务失败（返回空列表）: %s", exc)
        return out

    def get_transaction_body(self, task_id: str, tx_id: int) -> str:
        """按需取完整响应体（compressed 自动解压；digest 仅返回摘要）。

        供站点地图「点开单条事务查看完整响应」使用；不存在或读取失败返回空串。
        """
        try:
            with self._session_factory() as session:
                t = session.execute(
                    select(ReconHttpTransaction).where(
                        ReconHttpTransaction.id == tx_id,
                        ReconHttpTransaction.task_id == task_id,
                    )
                ).scalars().first()
                if t is None:
                    return ""
                if t.storage_strategy == "compressed" and t.body_compressed:
                    return gzip.decompress(t.body_compressed).decode(
                        "utf-8", errors="replace"
                    )
                return t.response_body or ""
        except Exception as exc:  # noqa: BLE001
            logger.warning("RECON 读取事务 body 失败: %s", exc)
            return ""

    def _http_tx_to_dict(self, t: "ReconHttpTransaction") -> Dict[str, object]:
        return {
            "id": t.id,
            "host": t.host,
            "path_normalized": t.path_normalized,
            "url": t.url,
            "method": t.method,
            "status_code": t.status_code,
            "source": t.source,
            "request_headers": t.request_headers,
            "request_body": t.request_body,
            "response_headers": t.response_headers,
            "response_body": t.response_body,
            "storage_strategy": t.storage_strategy,
            "body_available": t.storage_strategy in ("full", "compressed"),
            "body_blob_ref": t.body_blob_ref,
            "response_title": t.response_title,
            "content_type": t.content_type,
            "response_size": t.response_size,
            "response_time_ms": t.response_time_ms,
            "tech_stack": t.tech_stack,
            "detected_params": t.detected_params,
            "detected_forms": t.detected_forms,
            "auth_used": t.auth_used,
            "auth_required": t.auth_required,
            "created_at": _iso(t.created_at),
        }

    def list_fingerprints(
        self, task_id: str, limit: int = 200
    ) -> List[Dict[str, object]]:
        """返回指纹识别明细列表（前置侦查产物，四层结构化结果）。"""
        out: List[Dict[str, object]] = []
        try:
            with self._session_factory() as session:
                stmt = (
                    select(ReconFingerprint)
                    .where(ReconFingerprint.task_id == task_id)
                    .order_by(ReconFingerprint.updated_at.desc())
                    .limit(limit)
                )
                for f in session.execute(stmt).scalars().all():
                    out.append(self._fingerprint_to_dict(f))
        except Exception as exc:  # noqa: BLE001
            logger.warning("RECON 读取指纹失败（返回空列表）: %s", exc)
        return out

    def get_fingerprint(
        self, task_id: str, target_url: str
    ) -> Optional[Dict[str, object]]:
        """按目标 URL 精确读取一条指纹记录。"""
        try:
            with self._session_factory() as session:
                row = session.execute(
                    select(ReconFingerprint).where(
                        ReconFingerprint.task_id == task_id,
                        ReconFingerprint.target_url == target_url,
                    )
                ).scalar_one_or_none()
                return self._fingerprint_to_dict(row) if row else None
        except Exception as exc:  # noqa: BLE001
            logger.warning("RECON 读取指纹失败（返回 None）: %s", exc)
            return None

    def _fingerprint_to_dict(self, f: "ReconFingerprint") -> Dict[str, object]:
        return {
            "id": f.id,
            "target_url": f.target_url,
            "host": f.host,
            "status_code": f.status_code,
            "favicon_hash": f.favicon_hash,
            "auth_mode": f.auth_mode,
            "fingerprint": f.fingerprint_json,
            "matched_count": f.matched_count,
            "rule_count": f.rule_count,
            "source": f.source,
            "created_at": _iso(f.created_at),
            "updated_at": _iso(f.updated_at),
        }

    def list_threats(
        self,
        task_id: str,
        status: Optional[str] = None,
        severity: Optional[str] = None,
        limit: int = 500,
    ) -> List[Dict[str, object]]:
        """返回威胁态势列表，可选按状态/严重度过滤；evidence 自动解密。"""
        out: List[Dict[str, object]] = []
        try:
            with self._session_factory() as session:
                stmt = select(ReconThreat).where(ReconThreat.task_id == task_id)
                if status:
                    stmt = stmt.where(ReconThreat.status == status)
                if severity:
                    stmt = stmt.where(ReconThreat.severity == severity)
                stmt = stmt.order_by(
                    ReconThreat.cvss_score.desc().nullslast(),
                    ReconThreat.confidence.desc(),
                ).limit(limit)
                for t in session.execute(stmt).scalars().all():
                    out.append(self._threat_to_dict(t))
        except Exception as exc:  # noqa: BLE001
            logger.warning("RECON 读取威胁失败（返回空列表）: %s", exc)
        return out

    def get_threat(self, task_id: str, cve_id: str) -> Optional[Dict[str, object]]:
        """按 cve_id 取单威胁详情；未命中返回 None（调用方转 404）。"""
        try:
            with self._session_factory() as session:
                stmt = select(ReconThreat).where(ReconThreat.task_id == task_id)
                if cve_id:
                    stmt = stmt.where(ReconThreat.cve_id == cve_id)
                else:
                    stmt = stmt.where(ReconThreat.cve_id == "").order_by(
                        ReconThreat.confidence.desc()
                    )
                th = session.execute(stmt.limit(1)).scalars().first()
                if th is None:
                    return None
                return self._threat_to_dict(th)
        except Exception as exc:  # noqa: BLE001
            logger.warning("RECON 读取单威胁失败（返回 None）: %s", exc)
            return None

    def derived_assets(self, task_id: str) -> Dict[str, object]:
        """派生资产视图：主机清单（来自攻击面终端）+ 暴露服务入口（来自终端）。

        本地库无独立资产表，资产为 endpoints 的聚合投影，供前端资产面板：
        - hosts：按 host 聚合（端点数 / 技术栈 / 鉴权端点数 / 最高置信度）
        - services：每个终端视为一个暴露服务入口（host+path+method+状态码+技术栈）
        """
        hosts: List[Dict[str, object]] = []
        services: List[Dict[str, object]] = []
        try:
            with self._session_factory() as session:
                endpoints = session.execute(
                    select(ReconEndpoint).where(ReconEndpoint.task_id == task_id)
                ).scalars().all()
                host_map: Dict[str, Dict[str, object]] = {}
                for e in endpoints:
                    if not e.host:
                        continue
                    entry = host_map.get(e.host)
                    if entry is None:
                        entry = {
                            "host": e.host,
                            "endpoint_count": 0,
                            "tech_stack": [],
                            "auth_endpoints": 0,
                            "confidence": 0.0,
                        }
                        host_map[e.host] = entry
                    entry["endpoint_count"] = int(entry["endpoint_count"]) + 1  # type: ignore[arg-type]
                    entry["confidence"] = max(  # type: ignore[assignment]
                        float(entry["confidence"]), e.confidence or 0.0  # type: ignore[arg-type]
                    )
                    if e.auth_required:
                        entry["auth_endpoints"] = int(entry["auth_endpoints"]) + 1  # type: ignore[arg-type]
                    for t in e.tech_stack or []:
                        if t not in entry["tech_stack"]:  # type: ignore[operator]
                            entry["tech_stack"].append(t)  # type: ignore[arg-type]
                hosts = list(host_map.values())

                for e in endpoints:
                    services.append(
                        {
                            "host": e.host,
                            "path": e.path_normalized,
                            "method": e.method,
                            "status_code": e.status_code,
                            "tech_stack": list(e.tech_stack or []),
                            "confidence": e.confidence,
                        }
                    )
        except Exception as exc:  # noqa: BLE001
            logger.warning("RECON 读取派生资产失败（返回空结构）: %s", exc)
        return {"hosts": hosts, "services": services}

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------

    def _threat_to_dict(self, th: "ReconThreat") -> Dict[str, object]:
        """威胁 ORM → dict，evidence_summary 经 crypto 自动解密（明文降级原样）。"""
        evidence = th.evidence_summary or ""
        try:
            evidence = self.crypto.decrypt(evidence)
        except Exception:  # noqa: BLE001 - 解密失败保留密文，不阻断展示
            pass
        return {
            "id": th.id,
            "cve_id": th.cve_id,
            "title": th.title,
            "category": th.category,
            "severity": th.severity,
            "status": th.status,
            "cvss_score": th.cvss_score,
            "target_endpoint": th.affected_endpoint,
            "evidence_summary": evidence,
            "confidence": th.confidence,
            "created_at": _iso(th.created_at),
            "updated_at": _iso(th.updated_at),
        }


# ---------------------------------------------------------------------------
# 模块级辅助
# ---------------------------------------------------------------------------


def _sanitize_task_id(task_id: str) -> str:
    """清理 task_id 用于文件名，禁止路径分隔符与越界字符。"""
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", task_id)
    if not safe:
        raise ReconStoreError("task_id 清理后为空")
    return safe[:128]


def _restrict_file_perms(path: Path) -> None:
    """收敛文件权限为 0o600（仅属主读写），忽略不可写场景。"""
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _utcnow():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc)


def _iso(dt: Optional[object]) -> Optional[str]:
    """datetime → ISO8601 字符串（带 Z）；None 安全，非 datetime 原样返回。"""
    if dt is None:
        return None
    if isinstance(dt, str):
        return dt
    try:
        return dt.isoformat()  # type: ignore[attr-defined]
    except AttributeError:
        return str(dt)
