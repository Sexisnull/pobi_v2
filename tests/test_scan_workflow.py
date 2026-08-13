"""扫描工作流纯逻辑测试：不调用真实 LLM / 浏览器。

验证：
- model spec 构造（scheme/model_name 解析）
- 根域提取
- 决策解析（容错）
- 白盒阶段扩展点的降级（依赖缺失时优雅跳过）
"""
from __future__ import annotations

import asyncio

import pytest

from pobi_v2.engine import scan_workflow as sw
from pobi_v2.llm.config import get_model_spec


def test_model_spec_parses_scheme_and_name():
    # ScanWorkflow 的模型解析已统一收敛到 pobi_v2.llm.get_model_spec
    spec = get_model_spec("openai/gpt-4o")
    assert spec.provider == "openai"
    assert spec.model_name == "gpt-4o"


def test_model_spec_defaults_when_no_scheme():
    # 无 scheme 段（纯模型名简写）时按模型名启发式推断 provider：
    # claude-* -> anthropic，使 API key/base_url 从 ANTHROPIC_* 读取（与原 pobi_agent 真实行为一致）
    spec = get_model_spec("claude-3-5-sonnet")
    assert spec.provider == "anthropic"
    assert spec.model_name == "claude-3-5-sonnet"

    spec2 = get_model_spec("gpt-4o")
    assert spec2.provider == "openai"
    assert spec2.model_name == "gpt-4o"


def test_extract_root_domains():
    assert sw._extract_root_domains("https://shop.example.com/path") == ["shop.example.com"]
    assert sw._extract_root_domains("not a url") == []


def test_parse_decision_valid_json():
    text = '思考中...\n{"tool": "http_request", "done": false}\n结束'
    d = sw._parse_decision(text)
    assert d.get("tool") == "http_request"
    assert d.get("done") is False


def test_parse_decision_invalid_returns_empty():
    assert sw._parse_decision("没有任何 JSON 内容") == {}


def test_parse_decision_malformed_json_returns_empty():
    # 大括号不匹配时容错返回空
    assert sw._parse_decision("{ this is not json") == {}


def test_is_high_risk():
    assert sw._is_high_risk("run_shell")
    assert sw._is_high_risk("SQL_INJECTION")
    assert not sw._is_high_risk("read_file")


def test_whitebox_stage_import_failure_degrades_gracefully():
    """白盒阶段为可选能力：依赖（Playwright / Embedder）缺失时，
    resolve_whitebox_stage 应返回 None 而非抛异常，确保主流程不中断。"""

    async def _run():
        stage = await sw.resolve_whitebox_stage(enabled=True)
        return stage

    assert asyncio.run(_run()) is None


def test_whitebox_stage_disabled_returns_none():
    async def _run():
        return await sw.resolve_whitebox_stage(enabled=False)

    assert asyncio.run(_run()) is None
