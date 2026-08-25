"""威胁状态机单元测试：单向推进 / 降级拒绝 / exploited 必须带证据。"""

from __future__ import annotations

import pytest

from pobi_agent.recon import ReconStore, ReconStoreError
from pobi_agent.recon.sqlite_models import ReconThreat


def _threat_status(store, task_id, cve_id):
    with store._session_factory() as s:
        row = s.execute(
            __import__("sqlalchemy").select(ReconThreat).where(
                ReconThreat.cve_id == cve_id
            )
        ).scalar_one_or_none()
        return row.status if row else None


# ---------------------------------------------------------------------------
# 合法单向推进
# ---------------------------------------------------------------------------


def test_forward_migration_suspected_to_exploited(tmp_path):
    store = ReconStore(tmp_path / "st.db")
    store.ensure_session("t")
    store.upsert_threat("t", cve_id="CVE-2024-1001", status="suspected", confidence=0.3)

    assert store.update_threat_status("t", "CVE-2024-1001", "confirmed") is True
    assert _threat_status(store, "t", "CVE-2024-1001") == "confirmed"

    assert store.update_threat_status(
        "t", "CVE-2024-1001", "exploited", evidence_summary="flag{}"
    ) is True
    assert _threat_status(store, "t", "CVE-2024-1001") == "exploited"

    assert store.update_threat_status("t", "CVE-2024-1001", "remediated") is True
    assert _threat_status(store, "t", "CVE-2024-1001") == "remediated"
    store.close()


def test_same_status_idempotent(tmp_path):
    store = ReconStore(tmp_path / "st2.db")
    store.ensure_session("t")
    store.upsert_threat("t", cve_id="CVE-2024-1002", status="confirmed", confidence=0.7)
    # 同状态重复提交：返回 False（幂等），不抛错。
    assert store.update_threat_status("t", "CVE-2024-1002", "confirmed") is False
    store.close()


# ---------------------------------------------------------------------------
# 降级拒绝
# ---------------------------------------------------------------------------


def test_downgrade_rejected(tmp_path):
    store = ReconStore(tmp_path / "st3.db")
    store.ensure_session("t")
    store.upsert_threat("t", cve_id="CVE-2024-1003", status="exploited",
                        evidence_summary="ev", confidence=0.9)
    # exploited -> confirmed 降级被拒，状态保持不变。
    assert store.update_threat_status("t", "CVE-2024-1003", "confirmed") is False
    assert _threat_status(store, "t", "CVE-2024-1003") == "exploited"
    store.close()


# ---------------------------------------------------------------------------
# exploited 必须带证据
# ---------------------------------------------------------------------------


def test_exploited_requires_evidence(tmp_path):
    store = ReconStore(tmp_path / "st4.db")
    store.ensure_session("t")
    store.upsert_threat("t", cve_id="CVE-2024-1004", status="confirmed", confidence=0.7)
    with pytest.raises(ReconStoreError):
        store.update_threat_status("t", "CVE-2024-1004", "exploited", evidence_summary="")
    store.close()


# ---------------------------------------------------------------------------
# 非法状态 / 不存在威胁
# ---------------------------------------------------------------------------


def test_invalid_status_rejected(tmp_path):
    store = ReconStore(tmp_path / "st5.db")
    store.ensure_session("t")
    store.upsert_threat("t", cve_id="CVE-2024-1005", status="suspected")
    with pytest.raises(ReconStoreError):
        store.update_threat_status("t", "CVE-2024-1005", "fake_status")
    store.close()


def test_missing_threat_raises(tmp_path):
    store = ReconStore(tmp_path / "st6.db")
    store.ensure_session("t")
    with pytest.raises(ReconStoreError):
        store.update_threat_status("t", "CVE-NOT-EXIST", "confirmed")
    store.close()


# ---------------------------------------------------------------------------
# 空 cve_id 回退 affected_endpoint 定位
# ---------------------------------------------------------------------------


def test_empty_cve_falls_back_to_endpoint(tmp_path):
    store = ReconStore(tmp_path / "st7.db")
    store.ensure_session("t")
    store.upsert_threat("t", cve_id="", title="t1", category="cve",
                        affected_endpoint="/login", status="suspected", confidence=0.5)
    assert store.update_threat_status(
        "t", "", "confirmed", affected_endpoint="/login"
    ) is True
    assert _threat_status(store, "t", "") == "confirmed"
    store.close()


# ---------------------------------------------------------------------------
# reconcile_pg_to_local 反向对账
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reconcile_upgrades_and_keeps(tmp_path):
    import uuid

    from unittest.mock import AsyncMock, MagicMock

    # _task_id_of_db() 由 db 文件名反推，文件名须与 ensure_session 的 task_id 一致。
    store = ReconStore(tmp_path / "t.db")
    store.ensure_session("t")
    # 本地已 confirmed，PG 更高 exploited
    store.upsert_threat("t", cve_id="CVE-2024-2001", status="confirmed", confidence=0.7)

    agg = MagicMock()
    agg.cve_id = "CVE-2024-2001"
    agg.title = "t1"
    agg.category = "cve"
    agg.severity = "high"
    agg.status = "exploited"
    agg.cvss_score = 8.0
    agg.target_endpoint = "/x"
    agg.evidence_summary = "flag{}"
    agg.confidence = 0.9

    pg_session = AsyncMock()
    result_t = MagicMock()
    result_t.scalars.return_value.all.return_value = [agg]
    pg_session.execute.side_effect = [result_t]

    factory = MagicMock()
    factory.return_value.__aenter__.return_value = pg_session

    summary = await store.reconcile_pg_to_local(
        str(uuid.uuid4()), str(uuid.uuid4()), factory
    )
    assert summary["total"] == 1
    assert summary["upgraded"] == 1
    assert _threat_status(store, "t", "CVE-2024-2001") == "exploited"
    store.close()


@pytest.mark.asyncio
async def test_reconcile_seeds_missing_and_keeps_local_lead(tmp_path):
    import uuid

    from unittest.mock import AsyncMock, MagicMock

    store = ReconStore(tmp_path / "t.db")
    store.ensure_session("t")
    # 本地已 exploited（领先），PG 仅 confirmed
    store.upsert_threat("t", cve_id="CVE-2024-2002", status="exploited",
                        evidence_summary="ev", confidence=0.95)

    agg = MagicMock()
    agg.cve_id = "CVE-2024-2002"
    agg.title = "t2"
    agg.category = "cve"
    agg.severity = "high"
    agg.status = "confirmed"
    agg.cvss_score = 8.0
    agg.target_endpoint = "/x"
    agg.evidence_summary = ""
    agg.confidence = 0.7
    agg2 = MagicMock()
    agg2.cve_id = "CVE-2024-2003"
    agg2.title = "t3"
    agg2.category = "cve"
    agg2.severity = "medium"
    agg2.status = "confirmed"
    agg2.cvss_score = 5.0
    agg2.target_endpoint = "/y"
    agg2.evidence_summary = ""
    agg2.confidence = 0.6

    pg_session = AsyncMock()
    result_t = MagicMock()
    result_t.scalars.return_value.all.return_value = [agg, agg2]
    pg_session.execute.side_effect = [result_t]

    factory = MagicMock()
    factory.return_value.__aenter__.return_value = pg_session

    summary = await store.reconcile_pg_to_local(
        str(uuid.uuid4()), str(uuid.uuid4()), factory
    )
    assert summary["total"] == 2
    assert summary["kept_local"] == 1  # 本地 exploited 领先，保留
    assert summary["seeded"] == 1  # CVE-2024-2003 本地缺失，灌入
    assert _threat_status(store, "t", "CVE-2024-2002") == "exploited"
    assert _threat_status(store, "t", "CVE-2024-2003") == "confirmed"
    store.close()
