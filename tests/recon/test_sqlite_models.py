"""RECON 本地模型单元测试：枚举归一化 / 模型 CRUD / JSON 序列化 / 约束。"""

from __future__ import annotations

import pytest

from pobi_agent.recon.sqlite_models import (
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
from pobi_agent.recon.store import ReconStore, ReconStoreError


# ---------------------------------------------------------------------------
# 枚举归一化
# ---------------------------------------------------------------------------


def test_normalize_category_known():
    assert normalize_category("endpoint") is ReconCategory.endpoint
    assert normalize_category("ENDPOINT") is ReconCategory.endpoint


def test_normalize_category_alias():
    assert normalize_category("validated_exploit") is ReconCategory.validated_exploit
    assert normalize_category("vuln") is ReconCategory.vulnerability
    assert normalize_category("tech") is ReconCategory.technology


def test_normalize_category_unknown_falls_back_to_misc():
    assert normalize_category("totally_unknown_xyz") is ReconCategory.misc
    assert normalize_category("") is ReconCategory.misc


def test_normalize_category_passthrough_enum():
    assert normalize_category(ReconCategory.technology) is ReconCategory.technology


# ---------------------------------------------------------------------------
# 模型 CRUD 与 JSON 序列化
# ---------------------------------------------------------------------------


def test_session_and_fact_roundtrip(tmp_path):
    store = ReconStore(tmp_path / "t1.db")
    sid = store.ensure_session("task-1", target="http://x", objective="recon")
    assert isinstance(sid, int)

    store.upsert_fact(
        "task-1",
        "endpoint",
        "/admin",
        "login panel",
        confidence=0.9,
        details={"param": "user"},
    )
    with store._session_factory() as s:
        fact = s.execute(
            __import__("sqlalchemy").select(ReconFact).where(ReconFact.key == "/admin")
        ).scalar_one()
        assert fact.category == ReconCategory.endpoint.value
        assert fact.details_json == {"param": "user"}
        assert fact.session_id == sid
    store.close()


def test_endpoint_json_fields(tmp_path):
    store = ReconStore(tmp_path / "t2.db")
    store.ensure_session("task-2")
    store.upsert_endpoint(
        "task-2",
        "/api/login",
        host="example.com",
        tech_stack=["nginx", "php"],
        parameters=["user", "pass"],
        auth_required=True,
    )
    with store._session_factory() as s:
        ep = s.execute(
            __import__("sqlalchemy").select(ReconEndpoint).where(
                ReconEndpoint.path_normalized == "/api/login"
            )
        ).scalar_one()
        assert ep.tech_stack == ["nginx", "php"]
        assert ep.parameters == ["user", "pass"]
        assert ep.auth_required is True
    store.close()


def test_threat_status_enum_default(tmp_path):
    store = ReconStore(tmp_path / "t3.db")
    store.ensure_session("task-3")
    store.upsert_threat("task-3", cve_id="CVE-2024-0001", title="XSS", severity="high")
    with store._session_factory() as s:
        th = s.execute(
            __import__("sqlalchemy").select(ReconThreat).where(
                ReconThreat.cve_id == "CVE-2024-0001"
            )
        ).scalar_one()
        assert th.status == ThreatStatus.suspected.value
        assert th.severity == "high"
    store.close()
