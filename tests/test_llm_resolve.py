"""统一 LLM 解析入口（pobi_v2.llm）回归测试。

覆盖：解析优先级、平台统一前缀凭证优先、裸供应商变量兼容、provider 变量
归一、无 scheme 启发式推断、to_litellm_model 格式、deprecated 字段不被消费。
"""

import importlib
import os
import pytest

import pobi_v2.llm as llm_pkg
from pobi_v2.llm.config import get_model_spec, to_litellm_model


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """测试前后清理相关环境变量，并清空 lru_cache，避免互相污染。"""
    keys = [
        "POBI_V2_MODEL", "POBI_V2_LLM_API_KEY", "POBI_V2_LLM_API_BASE",
        "POBI_V2_LLM_MODEL",
        "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL",
        "OPENAI_API_KEY", "OPENAI_BASE_URL",
        "OPEN_ROUTER_API_KEY", "OPEN_ROUTER_BASE_URL",
        "GEMINI_API_KEY",
    ]
    saved = {k: os.environ.get(k) for k in keys}
    for k in keys:
        monkeypatch.delenv(k, raising=False)
    get_model_spec.cache_clear()
    importlib.reload(llm_pkg.config) if False else None
    yield
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    get_model_spec.cache_clear()


def _reload_settings(monkeypatch, **overrides):
    """用临时环境变量刷新 pobi_v2 全局 settings（POBI_V2_ 前缀）。"""
    for k, v in overrides.items():
        if v is None:
            monkeypatch.delenv(k, raising=False)
        else:
            monkeypatch.setenv(k, v)
    from pobi_v2.core.config import settings as s
    import pobi_v2.core.config as cfg
    importlib.reload(cfg)
    return cfg.settings


def test_scheme_split_and_litellm_format():
    spec = get_model_spec("anthropic/claude-sonnet-4")
    assert spec.provider == "anthropic"
    assert spec.model_name == "claude-sonnet-4"
    assert to_litellm_model(spec) == "anthropic/claude-sonnet-4"


def test_platform_prefix_key_takes_priority(monkeypatch):
    """POBI_V2_LLM_API_KEY 应优先于裸供应商变量。"""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-bare")
    monkeypatch.setenv("POBI_V2_LLM_API_KEY", "sk-platform")
    spec = get_model_spec("anthropic/claude-sonnet-4")
    assert spec.api_key == "sk-platform"


def test_bare_provider_var_compat_fallback(monkeypatch):
    """无平台前缀时，按 provider 回退到裸变量（兼容既有 .env）。"""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-bare")
    spec = get_model_spec("anthropic/claude-sonnet-4")
    assert spec.api_key == "sk-bare"


def test_openrouter_var_normalized(monkeypatch):
    """openrouter 一律读 OPEN_ROUTER_API_KEY（带下划线），消除 OPENROUTER 不一致。"""
    monkeypatch.setenv("OPEN_ROUTER_API_KEY", "sk-or")
    spec = get_model_spec("openrouter/anthropic/claude-3.5-sonnet")
    assert spec.provider == "openrouter"
    assert spec.api_key == "sk-or"


def test_base_url_platform_prefix_priority(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://bare")
    monkeypatch.setenv("POBI_V2_LLM_API_BASE", "https://platform")
    spec = get_model_spec("anthropic/claude-sonnet-4")
    assert spec.base_url == "https://platform"


def test_no_scheme_heuristic(monkeypatch):
    """无 scheme 纯模型名按关键字推断 provider。"""
    spec = get_model_spec("claude-3-5-sonnet")
    assert spec.provider == "anthropic"
    assert spec.model_name == "claude-3-5-sonnet"


def test_deprecated_llm_model_not_consumed(monkeypatch):
    """settings.llm_model（POBI_V2_LLM_MODEL）不再被解析入口消费，统一用 model。

    解析入口完全不读取 llm_model 字段；显式传入的 model 串优先于默认值。
    """
    # 设置 llm_model 为干扰值，验证它从不进入解析结果
    monkeypatch.setenv("POBI_V2_LLM_MODEL", "gemini/should-be-ignored")
    spec = get_model_spec("openai/gpt-4o")
    assert spec.provider == "openai"
    assert spec.model_name == "gpt-4o"
    # 即便 llm_model 设了 gemini，结果仍来自传入的 model，而非 llm_model
    assert spec.provider != "gemini"


def test_default_model_fallback(monkeypatch):
    """无显式串、无 POBI_V2_MODEL 时使用 settings 默认值。"""
    # settings.model 默认 "openai/gpt-4o-mini"
    spec = get_model_spec()
    assert spec.provider == "openai"
