"""prompt 跳过重复工作注入测试：覆盖块渲染与三阶段模板嵌入。"""

from __future__ import annotations

import uuid
from pathlib import Path

from pobi_agent.pobi_agent import DeadEndAgent
from pobi_agent.recon import ReconStore


def _lightweight_agent(tmp_path, seed=True):
    """用 __new__ 绕过重型 __init__，仅注入 recon_store 与 session id 用于测渲染。"""
    agent = DeadEndAgent.__new__(DeadEndAgent)
    store = ReconStore(tmp_path / "skip.db")
    store.ensure_session("t")
    if seed:
        store.upsert_endpoint("t", "/login", tech_stack=["nginx"])
        store.upsert_threat("t", cve_id="CVE-2024-3001", status="exploited",
                            evidence_summary="ev", confidence=0.9)
    agent.recon_store = store
    agent.embedding_session_id = "t"  # 与 store 内 task_id 一致（str() 后仍为 "t"）
    return agent


# ---------------------------------------------------------------------------
# _build_covered_block 渲染
# ---------------------------------------------------------------------------


def test_covered_block_rendered(tmp_path):
    agent = _lightweight_agent(tmp_path)
    block = agent._build_covered_block(token_budget=1000)
    assert "已覆盖资产" in block
    assert "/login" in block
    assert "CVE-2024-3001" in block
    assert "nginx" in block


def test_covered_block_empty_without_assets(tmp_path):
    agent = _lightweight_agent(tmp_path, seed=False)
    assert agent._build_covered_block() == ""


def test_covered_block_no_store(tmp_path):
    agent = DeadEndAgent.__new__(DeadEndAgent)
    agent.recon_store = None
    agent.embedding_session_id = "t"
    assert agent._build_covered_block() == ""


def test_covered_block_failure_degrades_to_empty(tmp_path):
    agent = _lightweight_agent(tmp_path, seed=True)
    agent.recon_store = None  # 模拟渲染失败路径（store 缺失）
    assert agent._build_covered_block() == ""


# ---------------------------------------------------------------------------
# 三阶段 prompt 模板嵌入（静态契约检查）
# ---------------------------------------------------------------------------


def test_supervisor_prompt_embeds_skip_block():
    import pobi_agent.pobi_agent as mod

    text = Path(mod.__file__).read_text(encoding="utf-8")
    # start_supervisor 模板：覆盖块 + 跳过规则
    assert "## 已覆盖资产（历史任务沉淀，本轮跳过重复工作）" not in text  # 块由 store 渲染
    assert "### 跳过重复工作规则（历史任务已覆盖，禁止重复劳动）" in text
    assert "禁止重复扫描、重复枚举、重复验证" in text


def test_exploit_context_embeds_skip_rule():
    import pobi_agent.pobi_agent as mod

    text = Path(mod.__file__).read_text(encoding="utf-8")
    # run_exploitation / start_testing_stream 的 exploit_context 均含跳过规则。
    assert text.count("### 跳过重复工作规则（历史任务已覆盖，禁止重复劳动）") >= 3
    assert "不再重复执行利用验证" in text


def test_start_testing_stream_includes_previous_context():
    import pobi_agent.pobi_agent as mod

    text = Path(mod.__file__).read_text(encoding="utf-8")
    # start_testing_stream 补齐 previous_context：模板含 Previous Reconnaissance Results。
    assert text.count("## Previous Reconnaissance Results") >= 2
