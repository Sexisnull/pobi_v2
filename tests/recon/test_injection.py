"""分层索引单元测试：build_index_view 的 L0/L1/L2 结构与 token 预算裁剪。"""

from __future__ import annotations

import pytest

from pobi_agent.recon import ReconStore
from pobi_agent.recon.sqlite_models import ThreatStatus
from pobi_agent.utils.functions import num_tokens_from_string


def _seed(store, task_id="t"):
    store.ensure_session(task_id)
    # L0 高置信度事实
    store.upsert_fact(task_id, "endpoint", "/admin", "panel", confidence=0.9)
    store.upsert_fact(task_id, "technology", "php", "backend", confidence=0.85)
    # L1 端点
    for i in range(5):
        store.upsert_endpoint(
            task_id, f"/api/v1/res{i}", host="h", tech_stack=["nginx"]
        )
    # L2 成功利用（confirmed 威胁 + 成功 technique）
    store.upsert_threat(
        task_id,
        cve_id="CVE-2024-1",
        title="SQLi",
        severity="high",
        status=ThreatStatus.confirmed,
        evidence_summary="union select worked",
    )
    store.upsert_technique(
        task_id, "SQLi UNION", category="execution",
        status="success", success_count=3, tested_count=4,
    )


def test_index_contains_l0_l1_l2(tmp_path):
    store = ReconStore(tmp_path / "idx.db")
    _seed(store)
    view = store.build_index_view("t")
    assert "## L0 目标基线" in view
    assert "## L1 端点与技术栈" in view
    assert "## L2 可复用利用经验" in view
    assert "/admin" in view  # L0 高置信度事实
    store.close()


def test_index_l2_includes_confirmed_threat(tmp_path):
    store = ReconStore(tmp_path / "idx2.db")
    _seed(store)
    view = store.build_index_view("t")
    assert "CVE-2024-1" in view
    assert "union select worked" in view
    assert "成功手法: SQLi UNION" in view
    store.close()


def test_index_token_budget_respected(tmp_path):
    store = ReconStore(tmp_path / "idx3.db")
    # 制造大量 L1 端点以触发预算裁剪。
    store.ensure_session("t")
    for i in range(50):
        store.upsert_endpoint("t", f"/p/{i}", host="h", tech_stack=["x" * 20])
    view = store.build_index_view("t")
    l1_section = view.split("## L1")[1].split("## L2")[0] if "## L2" in view else view.split("## L1")[1]
    assert num_tokens_from_string(l1_section) <= 500 + 5  # 容差
    store.close()


def test_index_empty_when_no_data(tmp_path):
    store = ReconStore(tmp_path / "idx4.db")
    store.ensure_session("t")
    view = store.build_index_view("t")
    # 仅 L0 存在但无高置信度事实，L1/L2 被裁剪为空 → 至少有一节 L0 头。
    assert "## L0" in view
    store.close()
