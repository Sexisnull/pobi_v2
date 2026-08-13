"""平台 LLM 统一解析与调用基础设施（litellm + instructor + tenacity）。

这是 pobi_v2 平台解析与调用 LLM 的唯一入口：所有执行路径必须经由
``get_model_spec`` 获取内核 ``ModelSpec``，``complete/complete_json/chat``
供平台自包含场景（前端问答、报告解读）使用。

凭证策略：优先平台统一前缀 ``POBI_V2_LLM_API_KEY`` / ``POBI_V2_LLM_API_BASE``，
缺失时按 provider 回退到裸供应商环境变量（兼容既有 .env）。
"""

from pobi_v2.llm.client import chat, complete, complete_json
from pobi_v2.llm.config import get_model_spec, to_litellm_model
from pobi_v2.llm.types import (
    LLMError,
    LLMMessage,
    LLMRequest,
    LLMResponse,
    ModelSpec,
    Role,
    UsageRecord,
)

__all__ = [
    "get_model_spec",
    "to_litellm_model",
    "complete",
    "complete_json",
    "chat",
    "LLMError",
    "LLMMessage",
    "LLMRequest",
    "LLMResponse",
    "ModelSpec",
    "Role",
    "UsageRecord",
]
