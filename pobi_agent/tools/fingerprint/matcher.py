"""指纹规则匹配引擎（纯函数，无 I/O）。

统一匹配 Finger/EHole 系指纹规则，支持三种 method：
- ``keyword``: 多关键字 AND 语义（全部命中才识别），作用于 header 或 body
- ``faviconhash``: mmh3 计算的 favicon 哈希精确匹配（keyword 列表任一命中）
- ``regula``: 正则任一命中（location 限定 header/body）

规则 schema（来自 data/fingerprints.json）::

    {
      "name": "wordpress",
      "method": "keyword",        # keyword | faviconhash | regula
      "location": "body",         # header | body（faviconhash 忽略）
      "keywords": ["wp-content"],
      "category": "cms",          # server | backend | frontend | cms | waf
      "confidence": 0.8
    }

匹配时 headers/body 统一小写化后比对，命中聚合同 (category, name) 取最高置信度。
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class FingerprintHit:
    """一条指纹命中结果（可追溯）。"""

    name: str
    category: str            # server | backend | frontend | cms | waf
    method: str              # keyword | faviconhash | regula
    confidence: float
    evidence: str = ""       # 命中信号说明（便于审计/追溯）


def _keywords_lower(rule: dict[str, object]) -> list[str]:
    return [str(k).lower() for k in (rule.get("keywords") or [])]


def match_rule(
    rule: dict[str, object],
    headers_text: str,
    body_lower: str,
    favicon_hash: int | None,
) -> FingerprintHit | None:
    """单条规则匹配；未命中返回 None。"""
    method = str(rule.get("method", "keyword"))
    name = str(rule.get("name", "")).strip()
    category = str(rule.get("category", "cms"))
    try:
        confidence = float(rule.get("confidence", 0.8))
    except (TypeError, ValueError):
        confidence = 0.8
    keywords = _keywords_lower(rule)
    if not name or not keywords:
        return None

    if method == "faviconhash":
        if favicon_hash is None:
            return None
        for k in keywords:
            if str(favicon_hash) == k:
                return FingerprintHit(
                    name, category, method, confidence,
                    evidence=f"favicon hash {favicon_hash}",
                )
        return None

    location = str(rule.get("location", "body"))
    text = (headers_text if location == "header" else body_lower).lower()

    if method == "regula":
        for pat in keywords:
            try:
                if re.search(pat, text):
                    return FingerprintHit(
                        name, category, method, confidence,
                        evidence=f"regex /{pat}/ in {location}",
                    )
            except re.error:
                continue
        return None

    # keyword（默认）：全部关键字 AND
    if all(k in text for k in keywords):
        return FingerprintHit(
            name, category, method, confidence,
            evidence=f"keywords {keywords} in {location}",
        )
    return None


def match_rules(
    rules: list[dict[str, object]],
    headers_text: str,
    body_lower: str,
    favicon_hash: int | None = None,
) -> list[FingerprintHit]:
    """对全部规则匹配，聚合同 (category, name) 取最高置信度，按置信度降序。"""
    hits: dict[tuple, FingerprintHit] = {}
    for rule in rules:
        hit = match_rule(rule, headers_text, body_lower, favicon_hash)
        if hit is None:
            continue
        key = (hit.category, hit.name)
        prev = hits.get(key)
        if prev is None or hit.confidence > prev.confidence:
            hits[key] = hit
    return sorted(hits.values(), key=lambda h: h.confidence, reverse=True)
