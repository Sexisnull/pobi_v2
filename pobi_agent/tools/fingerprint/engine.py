"""指纹识别引擎：前置侦查与（旧）agent 工具共用的无上下文核心。

本模块不依赖 RunContext，仅需 target 与可选 cookies/headers，即可完成
采集 → 匹配 → 聚合 → 落库 全流程。前置侦查通道 ``pobi_v2.engine.pre_recon``
与旧 ``webapp_fingerprint`` 工具均通过本模块执行，避免两套逻辑漂移。

落库口径（recon 五表 + fingerprint 明细表）：
- ``recon_fingerprints``：完整结构化四层明细（幂等键 task_id+target_url）
- ``recon_facts(category=technology)``：精炼结论，供 L0/L1 注入下游
- ``recon_endpoints(tech_stack)``：技术栈挂载到 "/"
- ``recon_techniques(fingerprint|{host})``：足迹防重，同 host 只探测一次
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pobi_agent.recon.store import ReconStore

from .collector import collect, normalize_target
from .detectors import detect_layers
from .matcher import FingerprintHit, match_rules

logger = logging.getLogger(__name__)

# 四层输出顺序（与前端 / LLM 展示一致）
LAYERS: tuple[str, ...] = ("server", "backend", "frontend", "cms", "waf")

_RULES_PATH = Path(__file__).parent / "data" / "fingerprints.json"
_rules_cache: list[dict[str, Any]] | None = None


def load_rules() -> list[dict[str, Any]]:
    """加载指纹库（cms + waf），模块级缓存避免重复读盘。"""
    global _rules_cache
    if _rules_cache is None:
        try:
            doc = json.loads(_RULES_PATH.read_text(encoding="utf-8"))
            _rules_cache = doc.get("rules", [])
        except Exception as exc:  # noqa: BLE001 - 指纹库缺失不阻断
            logger.warning("指纹库加载失败: %s", exc)
            _rules_cache = []
    return _rules_cache


def rule_count() -> int:
    """指纹库规则总数（供落库 rule_count 字段）。"""
    return len(load_rules())


@dataclass
class FingerprintResult:
    """一次指纹探测的结构化结果。"""

    target: str
    status_code: int | None
    favicon_hash: int | None
    fingerprints: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    matched_count: int = 0
    rule_count: int = 0
    auth_mode: str = "external_only"


def group_hits(hits: list[FingerprintHit]) -> dict[str, list[dict[str, Any]]]:
    """按 category 分组，输出面向 LLM / 落库的精炼结构。"""
    grouped: dict[str, list[dict[str, Any]]] = {layer: [] for layer in LAYERS}
    for h in hits:
        item = {
            "name": h.name,
            "confidence": round(h.confidence, 2),
            "method": h.method,
            "evidence": h.evidence,
        }
        grouped.setdefault(h.category, []).append(item)
    return grouped


async def run_fingerprint(
    target: str,
    cookies: dict[str, str] | None = None,
    extra_headers: dict[str, str] | None = None,
    auth_mode: str = "external_only",
) -> FingerprintResult:
    """对目标执行一次完整指纹探测（采集 + 四层匹配），无落库副作用。

    Args:
        target: 目标 URL（自动补 scheme）。
        cookies: 附加 Cookie（登录后探测用）。
        extra_headers: 附加请求头（auth headers 等）。
        auth_mode: 认证模式标记，随结果落库。

    Returns:
        FingerprintResult：四层指纹（server/backend/frontend/cms/waf）。
    """
    target = normalize_target(target)
    signals = await collect(target, cookies, extra_headers)
    # server/backend/frontend 分层内建特征 + cms/waf 指纹库规则
    layer_hits = detect_layers(
        signals.headers_text, signals.body_lower, signals.favicon_hash
    )
    lib_hits = match_rules(
        load_rules(), signals.headers_text, signals.body_lower, signals.favicon_hash
    )
    all_hits = layer_hits + lib_hits
    return FingerprintResult(
        target=target,
        status_code=signals.status_code,
        favicon_hash=signals.favicon_hash,
        fingerprints=group_hits(all_hits),
        matched_count=len(all_hits),
        rule_count=len(load_rules()),
        auth_mode=auth_mode,
    )


def _host_of(target: str) -> str:
    """从 URL 提取 host（含端口），供足迹键与 host 字段使用。"""
    from urllib.parse import urlparse

    return urlparse(target).netloc or target


def persist_fingerprint(
    store: ReconStore,
    task_id: str,
    result: FingerprintResult,
    *,
    source: str = "pre_recon",
) -> None:
    """把指纹结果幂等落库（明细表 + facts + endpoints + 足迹）。

    失败仅记 warning，不阻断前置侦查主流程。
    """
    host = _host_of(result.target)
    try:
        # 1) fingerprint 明细表（完整结构化结果）
        store.upsert_fingerprint(
            task_id=task_id,
            target_url=result.target,
            host=host,
            status_code=result.status_code,
            favicon_hash=result.favicon_hash,
            auth_mode=result.auth_mode,
            fingerprint=result.fingerprints,
            matched_count=result.matched_count,
            rule_count=result.rule_count,
            source=source,
        )
        # 2) technology facts（供 L0/L1 注入）
        flat: list[tuple[str, str, float]] = []
        for layer, items in result.fingerprints.items():
            for it in items:
                flat.append((layer, it["name"], float(it["confidence"])))
        for layer, name, conf in flat:
            store.upsert_fact(
                task_id=task_id,
                category="technology",
                key=f"{layer}:{name}",
                value=f"{name}（{layer}），匹配方式 keyword/faviconhash/regula",
                confidence=conf,
                source=source,
                details={
                    "category": layer,
                    "method": "fingerprint",
                    "auth_mode": result.auth_mode,
                },
            )
        # 端点树不再双写：技术栈信息保留在 recon_facts(technology) 中（供 L0/L1 与
        # 基线块消费），端点树由 ReconStore.derive_endpoints_from_transactions 在
        # recon_http_transactions 之上统一派生。
        # 3) 足迹防重：同 host 只探测一次
        store.upsert_technique(
            task_id=task_id,
            name=f"fingerprint|{host}",
            category="fingerprint",
            status="completed",
            success_count=1,
            last_result=f"四层指纹扫描完成，命中 {result.matched_count} 条",
            confidence=0.9,
        )
    except Exception as exc:  # noqa: BLE001 - 落库失败不阻断
        logger.warning("指纹结果落库失败（已忽略）: %s", exc)
