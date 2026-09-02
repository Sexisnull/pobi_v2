"""统一 LLM 模型规格解析（平台唯一入口）。

本模块是 pobi_v2 平台解析 LLM 配置的唯一入口，所有执行路径（M8 主路径、
M7 降级、默认 agent 路径、平台自包含问答/报告）都必须经此解析，禁止在别处
自行 ``os.environ.get`` 读 LLM 凭证，从而消除「同一 provider 在主/降级路径读
不同环境变量名」「POBI_V2_LLM_API_KEY 配了不生效」两类分叉。

解析策略（单一前缀 + 裸变量兼容）：
    - 模型名来自 ``model_str``（优先）或 ``settings.model``（POBI_V2_MODEL），
      形如 ``"provider/model"``，provider 段仅决定 litellm 路由前缀。
    - 凭证优先取平台统一前缀 ``POBI_V2_LLM_API_KEY`` / ``POBI_V2_LLM_API_BASE``，
      缺失时按 provider 回退到裸供应商环境变量（``ANTHROPIC_API_KEY`` 等），
      兼容用户既有 .env。
    - 同一 provider 在所有路径统一只读「同一个」裸变量名（如 openrouter 一律
      ``OPEN_ROUTER_API_KEY``，而非 ``OPENROUTER_API_KEY``），避免回归不一致。

产出内核 ``pobi_agent.config.settings.ModelSpec``（pydantic），内核
PobiAgent 直接消费，彻底接入内核，不再使用本包自有的 dataclass。
"""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Optional

from pobi_agent.config.settings import ModelSpec

from pobi_v2.core.config import settings


# provider -> 裸供应商凭证/Base URL 环境变量（统一名，openrouter 用 OPEN_ROUTER_API_KEY）
_PROVIDER_ENV: dict[str, tuple[Optional[str], Optional[str]]] = {
    "anthropic": ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL"),
    "openai": ("OPENAI_API_KEY", "OPENAI_BASE_URL"),
    "open_router": ("OPEN_ROUTER_API_KEY", "OPEN_ROUTER_BASE_URL"),
    "openrouter": ("OPEN_ROUTER_API_KEY", "OPEN_ROUTER_BASE_URL"),
    "gemini": ("GEMINI_API_KEY", "GEMINI_BASE_URL"),
    "google": ("GEMINI_API_KEY", "GEMINI_BASE_URL"),
    "requesty": ("REQUESTY_API_KEY", "REQUESTY_BASE_URL"),
    "local": ("LOCAL_API_KEY", "LOCAL_BASE_URL"),
}


def _env(key: str, default: Optional[str] = None) -> Optional[str]:
    """读取环境变量；空串视为未设置。"""
    val = os.getenv(key)
    if val is None or val == "":
        return default
    return val


def _split_model(model_str: str) -> tuple[str, str]:
    """把 ``provider/model`` 拆为 (provider, model_name)；无 ``/`` 时按关键字推断。"""
    raw = (model_str or "").strip()
    if "/" in raw:
        provider, model_name = raw.split("/", 1)
        return provider.lower(), model_name

    # 无 scheme：纯模型名简写，按关键字启发式推断 provider
    lower = raw.lower()
    for hint, provider in (
        ("claude", "anthropic"),
        ("gpt", "openai"),
        ("o1", "openai"),
        ("o3", "openai"),
        ("gemini", "google"),
        ("deepseek", "deepseek"),
        ("qwen", "openai"),
        ("doubao", "openai"),
    ):
        if hint in lower:
            return provider, raw
    return "openai", raw


@lru_cache(maxsize=1)
def _default_model_str() -> str:
    """默认模型串（来自平台配置 POBI_V2_MODEL）。"""
    return settings.model or "openai/gpt-4o-mini"


@lru_cache(maxsize=32)
def get_model_spec(model_str: Optional[str] = None) -> ModelSpec:
    """平台唯一 LLM 解析入口，产出内核 ``ModelSpec``。

    解析优先级：
        1. 显式 ``model_str`` 优先，否则 ``settings.model``（POBI_V2_MODEL）。
        2. 凭证：``POBI_V2_LLM_API_KEY`` 优先，缺失按 provider 裸变量回退；
           Base URL 同理（``POBI_V2_LLM_API_BASE`` 优先，裸变量回退）。

    统一入口保证：同一个 provider 在所有路径读同一个裸变量名，消除分叉。
    """
    raw = (model_str or _default_model_str()).strip()
    if not raw:
        raise ValueError(
            "未配置任何 LLM 模型。请设置 POBI_V2_MODEL（形如 'provider/model'）。"
        )

    provider, model_name = _split_model(raw)
    key_env, url_env = _PROVIDER_ENV.get(provider, (None, None))

    # 凭证：平台统一前缀优先，裸供应商变量兜底（兼容既有 .env）
    api_key = (
        _env("POBI_V2_LLM_API_KEY")
        or settings.llm_api_key
        or (_env(key_env) if key_env else None)
    )
    base_url = (
        _env("POBI_V2_LLM_API_BASE")
        or settings.llm_api_base
        or (_env(url_env) if url_env else None)
    )

    return ModelSpec(
        provider=provider,
        model_name=model_name,
        api_key=api_key,
        base_url=base_url,
    )


def to_litellm_model(spec: ModelSpec) -> str:
    """内核 ``ModelSpec`` -> litellm 的 ``"provider/model"`` 调用名。"""
    provider = (spec.provider or "openai").strip()
    model_name = (spec.model_name or "").strip()
    # 若 model_name 已是 "provider/model" 形式，保持原样
    if "/" in model_name:
        return model_name
    return f"{provider}/{model_name}"


import logging  # noqa: E402  (模块末尾统一设置 LLM 全局行为)

_log = logging.getLogger("pobi")


def configure_litellm() -> None:
    """为所有经 litellm 的 LLM 调用设置全局超时与重试。

    agent 路径（pydantic_ai -> litellm）与平台路径（pobi_v2.llm.client）最终都
    调用 litellm，故在此统一设置即可同时覆盖，避免「远端无响应时协程无限 await
    假死、任务只能等 30 分钟子超时」的问题。超时后 litellm 会按 llm_max_retries
    自动退避重试瞬态错误（超时 / 限流 / 5xx / 连接失败），仍失败才向上抛异常，
    由 executor 兜底归类为『用户可读的 LLM 失败原因』。
    """
    import litellm  # 延迟导入：仅在本函数被调用时加载，避免无谓的启动开销

    timeout = int(getattr(settings, "llm_request_timeout", 180) or 180)
    retries = int(getattr(settings, "llm_max_retries", 3) or 3)
    try:
        litellm.request_timeout = timeout
    except Exception:  # noqa: BLE001 — 不同 litellm 版本属性名略有差异，容忍
        pass
    try:
        litellm.num_retries = retries
    except Exception:  # noqa: BLE001
        pass
    try:
        retry_cfg = getattr(litellm, "retry", None)
        if isinstance(retry_cfg, dict):
            retry_cfg["num_retries"] = retries
            retry_cfg["backoff_factor"] = 2
    except Exception:  # noqa: BLE001
        pass
    _log.info(
        "[LLM] litellm 全局配置生效 | request_timeout=%ss | num_retries=%s",
        timeout, retries,
    )


# 模块导入即生效：worker / API / reconcile 等任何导入本层（进而触发 LLM 调用）的
# 进程都会先应用超时与重试，无需各自在启动钩子里重复调用。
configure_litellm()
