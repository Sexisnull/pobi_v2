"""seed_from_pg 增量续扫单元测试：覆盖清单 / 幂等统计 / 高置信度覆盖。"""

from __future__ import annotations

import itertools
from unittest.mock import AsyncMock, MagicMock

import pytest

from pobi_agent.recon import ReconStore
from pobi_agent.recon.sqlite_models import ReconFact, ReconThreat


# ---------------------------------------------------------------------------
# seed_from_pg 增量语义
# ---------------------------------------------------------------------------


def _pg_agg_fact(category, key, value="v", confidence=0.7):
    f = MagicMock()
    f.category = category
    f.key = key
    f.value = value
    f.confidence = confidence
    f.details_json = {}
    return f


def _pg_agg_threat(cve_id, title="t", category="cve", severity="high", status="confirmed",
                   cvss_score=7.5, target_endpoint="/x", evidence_summary="ev", confidence=0.8):
    t = MagicMock()
    t.cve_id = cve_id
    t.title = title
    t.category = category
    t.severity = severity
    t.status = status
    t.cvss_score = cvss_score
    t.target_endpoint = target_endpoint
    t.evidence_summary = evidence_summary
    t.confidence = confidence
    return t


@pytest.mark.asyncio
async def test_seed_counts_new_and_covered(tmp_path):
    # 注意：_task_id_of_db() 从 db 文件名反推 task_id，文件名须与 ensure_session 的 task_id 一致。
    store = ReconStore(tmp_path / "t.db")
    store.ensure_session("t")

    pg_session = AsyncMock()
    result_e = MagicMock()
    result_e.scalars.return_value.all.return_value = []  # recon_endpoints_agg：本轮无资产
    result_f = MagicMock()
    result_f.scalars.return_value.all.return_value = [
        _pg_agg_fact("endpoint", "/api/v1"),
        _pg_agg_fact("technology", "nginx"),
    ]
    result_t = MagicMock()
    result_t.scalars.return_value.all.return_value = [
        _pg_agg_threat("CVE-2024-0001", status="confirmed"),
    ]
    # seed_from_pg 依次查询 endpoints → facts → threats 三张聚合表；cycle 保证二次 seed 复用同序。
    pg_session.execute.side_effect = itertools.cycle([result_e, result_f, result_t])

    factory = MagicMock()
    factory.return_value.__aenter__.return_value = pg_session

    res = await store.seed_from_pg("11111111-1111-1111-1111-111111111111",
                                   "22222222-2222-2222-2222-222222222222", factory)
    assert res.seeded_count == 3
    assert res.already_covered_count == 0
    assert "/api/v1" in res.covered_endpoints
    assert "CVE-2024-0001" in res.covered_threats

    # 二次 seed：全部已覆盖，seeded=0
    res2 = await store.seed_from_pg("11111111-1111-1111-1111-111111111111",
                                    "22222222-2222-2222-2222-222222222222", factory)
    assert res2.seeded_count == 0
    assert res2.already_covered_count == 3
    assert res2.covered_endpoints == ["/api/v1"]
    store.close()


@pytest.mark.asyncio
async def test_seed_higher_confidence_overwrites(tmp_path):
    store = ReconStore(tmp_path / "t.db")
    store.ensure_session("t")
    store.upsert_fact("t", "technology", "nginx", "old", confidence=0.4)

    pg_session = AsyncMock()
    result_e = MagicMock()
    result_e.scalars.return_value.all.return_value = []  # endpoints 聚合：空
    result_f = MagicMock()
    result_f.scalars.return_value.all.return_value = [
        _pg_agg_fact("technology", "nginx", value="new", confidence=0.95),
    ]
    result_t = MagicMock()
    result_t.scalars.return_value.all.return_value = []
    pg_session.execute.side_effect = itertools.cycle([result_e, result_f, result_t])

    factory = MagicMock()
    factory.return_value.__aenter__.return_value = pg_session

    res = await store.seed_from_pg("11111111-1111-1111-1111-111111111111",
                                   "22222222-2222-2222-2222-222222222222", factory)
    # nginx 已存在：计入 already_covered，但高置信度覆盖内容。
    assert res.already_covered_count == 1
    assert res.seeded_count == 0
    with store._session_factory() as s:
        row = s.execute(
            __import__("sqlalchemy").select(ReconFact).where(ReconFact.key == "nginx")
        ).scalar_one()
        assert row.value == "new"
        assert row.confidence == 0.95
    store.close()


# ---------------------------------------------------------------------------
# covered_assets 只读统计
# ---------------------------------------------------------------------------


def test_covered_assets_lists_assets(tmp_path):
    store = ReconStore(tmp_path / "cov.db")
    store.ensure_session("t")
    store.upsert_endpoint("t", "/login", tech_stack=["nginx"])
    store.upsert_endpoint("t", "/admin", tech_stack=["php"])
    store.upsert_threat("t", cve_id="CVE-2024-0002", status="confirmed", confidence=0.9)
    store.upsert_threat("t", cve_id="CVE-2024-0003", status="suspected", confidence=0.3)

    cov = store.covered_assets("t")
    assert set(cov.covered_endpoints) == {"/login", "/admin"}
    assert set(cov.covered_techniques) == {"nginx", "php"}
    # 只统计 confirmed/exploited 威胁
    assert cov.covered_threats == ["CVE-2024-0002"]
    assert cov.seeded_count == 0
    store.close()


def test_covered_assets_empty(tmp_path):
    store = ReconStore(tmp_path / "cov2.db")
    store.ensure_session("t")
    cov = store.covered_assets("t")
    assert not cov.covered_endpoints
    assert not cov.covered_threats
    assert not cov
    store.close()


# ---------------------------------------------------------------------------
# build_covered_block 渲染
# ---------------------------------------------------------------------------


def test_build_covered_block(tmp_path):
    store = ReconStore(tmp_path / "blk.db")
    store.ensure_session("t")
    store.upsert_endpoint("t", "/login", tech_stack=["nginx"])
    store.upsert_threat("t", cve_id="CVE-2024-0004", status="exploited",
                        evidence_summary="ev", confidence=0.9)
    block = store.build_covered_block("t")
    assert "已覆盖资产" in block
    assert "/login" in block
    assert "nginx" in block
    assert "CVE-2024-0004" in block
    store.close()


def test_build_covered_block_empty(tmp_path):
    store = ReconStore(tmp_path / "blk2.db")
    store.ensure_session("t")
    assert store.build_covered_block("t") == ""
    store.close()


def test_build_covered_block_respects_budget(tmp_path):
    store = ReconStore(tmp_path / "blk3.db")
    store.ensure_session("t")
    for i in range(200):
        store.upsert_endpoint("t", f"/ep{i}", tech_stack=["nginx"])
    block = store.build_covered_block("t", token_budget=100)
    assert len(block) < 1000  # 预算内确定性裁剪
    store.close()
