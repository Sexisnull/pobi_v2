"""Sitemap 引擎（前置侦查专用）：katana JSONL 解析 + 去噪 + 落库。

与 fingerprint 引擎对称：本模块不依赖 RunContext 与 Kali 沙箱（纯解析/落库），
Kali 内执行 katana 由 ``pobi_v2.engine.sitemap.katana_runner`` 负责，pre_recon
调用 ``run_sitemap`` 完成「执行 → 解析 → 去噪 → 落库」全链路。

落库口径（与 requester 对齐的单一真源）：
- ``recon_http_transactions``：每次请求一条流水（保留历史可对比，katana/requester 同表）
- ``recon_endpoints``：去重后的端点树（幂等键 task_id+path_normalized）
- ``recon_techniques(sitemap|{host})``：足迹防重，同 host 只跑一次

无意义页面三层防线之一（落库层）：静态资源 / 登出错误噪音 / 纯分页参数变体在此过滤，
与 katana 参数层（-ef / -iqp）互补（README 无 -pcs/-fsu/-filter-page-type）。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from pobi_agent.recon.store import ReconStore
from pobi_agent.utils.urls import (
    extract_params,
    has_noise_params,
    is_noise_path,
    is_static_asset,
    normalize_path,
)

logger = logging.getLogger(__name__)

# 认证重定向目标特征（302/303/307/308 → login 判定 auth_required）。
_LOGIN_PATH_RE = re.compile(r"/(?:login|signin|auth|sso)(?:[/?#]|$)", re.IGNORECASE)

# 站点头部提取（response headers 键名可能为小写/原始两种）。
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)


@dataclass
class KatanaEntry:
    """katana JSONL 单行解析后的结构化事务。"""

    url: str
    method: str = "GET"
    status_code: int | None = None
    request_headers: dict[str, Any] = field(default_factory=dict)
    request_body: str = ""
    response_headers: dict[str, Any] = field(default_factory=dict)
    response_body: str = ""
    technologies: list[str] = field(default_factory=list)
    timestamp: str = ""


def parse_katana_jsonl(text: str) -> list[KatanaEntry]:
    """解析 katana ``-j`` JSONL 输出（逐行容错，坏行跳过）。

    katana 单行结构：{"timestamp","request":{"method","endpoint","raw","headers","body"},
    "response":{"status_code","headers","body","technologies","raw"}}。
    """
    if not text:
        return []
    entries: list[KatanaEntry] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            doc = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(doc, dict):
            continue
        req = doc.get("request") or {}
        res = doc.get("response") or {}
        if not isinstance(req, dict) or not isinstance(res, dict):
            continue
        url = (req.get("endpoint") or "").strip()
        if not url:
            continue
        entries.append(
            KatanaEntry(
                url=url,
                method=str(req.get("method") or "GET").upper(),
                status_code=res.get("status_code"),
                request_headers=dict(req.get("headers") or {}),
                request_body=req.get("body") or "",
                response_headers=dict(res.get("headers") or {}),
                response_body=res.get("body") or "",
                technologies=list(res.get("technologies") or []),
                timestamp=doc.get("timestamp") or "",
            )
        )
    return entries


def _host_of(url: str) -> str:
    return urlparse(url).netloc or url


def _status_auth_required(entry: KatanaEntry) -> bool:
    """从状态码 / 重定向目标判定 auth_required。

    - 401 / 407 → 明确需认证
    - 403 → 可能需认证（访问被拒，标 auth_required 供 agent 复用认证后重测）
    - 3xx 且 Location 指向 /login 等 → 会话跳转（未认证）
    """
    sc = entry.status_code or 0
    if sc in (401, 407):
        return True
    if sc == 403:
        return True
    if sc in (301, 302, 303, 307, 308):
        loc = ""
        for k, v in (entry.response_headers or {}).items():
            if str(k).lower() == "location":
                loc = str(v)
                break
        if loc and _LOGIN_PATH_RE.search(urlparse(loc).path or loc):
            return True
    return False


def _header_get(headers: dict[str, Any], name: str) -> str:
    for k, v in (headers or {}).items():
        if str(k).lower() == name.lower():
            return str(v)
    return ""


def _response_title(entry: KatanaEntry) -> str:
    m = _TITLE_RE.search(entry.response_body or "")
    if m:
        return re.sub(r"\s+", " ", m.group(1)).strip()[:512]
    return ""


def should_keep(url: str) -> bool:
    """无意义页面过滤（落库第二层防线）。

    丢弃：静态资源扩展名 / 登出·错误·会话噪音路径 / 纯分页·排序参数变体。
    保留：其余端点（含 403/404/302 —— 反映端点存在性与认证面）。
    """
    if is_static_asset(url):
        return False
    if is_noise_path(url):
        return False
    if has_noise_params(url):
        return False
    return True


def persist_sitemap(
    store: ReconStore,
    task_id: str,
    entries: list[KatanaEntry],
    *,
    source: str = "sitemap:katana",
    auth_used: bool = False,
    session_id: int | None = None,
    host_hint: str = "",
) -> dict[str, Any]:
    """把 katana 解析结果幂等落库（transactions + endpoints + 足迹）。

    失败仅记 warning，不阻断前置侦查主流程。返回统计供实时流展示。

    Returns:
        {"total", "kept", "dropped", "endpoints", "transactions", "auth_required"}
    """
    stats: dict[str, Any] = {
        "total": len(entries),
        "kept": 0,
        "dropped": 0,
        "transactions": 0,
        "auth_required": 0,
    }
    try:
        for entry in entries:
            if not should_keep(entry.url):
                stats["dropped"] += 1
                continue
            stats["kept"] += 1
            host = _host_of(entry.url) or host_hint
            path = normalize_path(entry.url)
            auth_required = _status_auth_required(entry)
            if auth_required:
                stats["auth_required"] += 1
            # 1) 事务流水（保留每次请求，含响应体摘要/外置）
            store.insert_http_transaction(
                task_id=task_id,
                host=host,
                path_normalized=path,
                url=entry.url,
                method=entry.method,
                status_code=entry.status_code,
                source=source,
                request_headers=entry.request_headers,
                request_body=entry.request_body,
                response_headers=entry.response_headers,
                response_body=entry.response_body,
                response_title=_response_title(entry),
                content_type=_header_get(entry.response_headers, "Content-Type"),
                response_size=len(entry.response_body or ""),
                tech_stack=entry.technologies,
                detected_params=extract_params(entry.url),
                auth_used=auth_used,
                auth_required=auth_required,
                session_id=session_id,
            )
            stats["transactions"] += 1
        # 端点树不再双写：统一由 ReconStore.derive_endpoints_from_transactions
        # 在 recon_http_transactions 之上派生（避免 sitemap/requester 双写不一致，
        # 并天然收敛 PG 同步唯一键）。pre_recon 落库完成后调用派生入口。
        # 3) 足迹防重：同 host 只跑一次 sitemap
        host_key = host_hint
        if not host_key and entries:
            host_key = _host_of(entries[0].url)
        if host_key:
            store.upsert_technique(
                task_id=task_id,
                name=f"sitemap|{host_key}",
                category="sitemap",
                status="completed",
                success_count=1,
                tested_count=1,
                last_result=f"站点地图构建完成，抓取 {stats['total']} 条，保留 {stats['kept']} 条",
                confidence=0.9,
                session_id=session_id,
            )
    except Exception as exc:  # noqa: BLE001 - 落库失败不阻断
        logger.warning("sitemap 落库失败（已忽略）: %s", exc)
    return stats
