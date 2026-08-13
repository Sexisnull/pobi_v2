"""平台自包含 LLM 调用封装：litellm + instructor + tenacity。"""

from __future__ import annotations

import time
from typing import Any, Optional, Type, TypeVar

import litellm
from instructor import from_litellm
from pydantic import BaseModel
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from pobi_agent.config.settings import ModelSpec
from pobi_v2.core.config import settings
from pobi_v2.llm.config import get_model_spec, to_litellm_model
from pobi_v2.llm.types import (
    LLMError,
    LLMMessage,
    LLMRequest,
    LLMResponse,
    Role,
    UsageRecord,
)

T = TypeVar("T", bound=BaseModel)

_instructor_client = from_litellm(litellm.acompletion)


def _to_messages(msgs: list[LLMMessage]) -> list[dict]:
    out = []
    for m in msgs:
        item: dict[str, Any] = {"role": m.role, "content": m.content}
        if m.name:
            item["name"] = m.name
        out.append(item)
    return out


def _usage(resp: Any) -> UsageRecord:
    try:
        u = resp.usage
        return UsageRecord(
            prompt_tokens=getattr(u, "prompt_tokens", 0) or 0,
            completion_tokens=getattr(u, "completion_tokens", 0) or 0,
            total_tokens=getattr(u, "total_tokens", 0) or 0,
        )
    except Exception:
        return UsageRecord()


@retry(
    retry=retry_if_exception_type((litellm.RateLimitError, litellm.Timeout, litellm.APIConnectionError)),
    wait=wait_exponential(multiplier=1, min=1, max=30),
    stop=stop_after_attempt(6),
    reraise=True,
)
async def complete(req: LLMRequest) -> LLMResponse:
    """文本补全（带限流与重试）。"""
    t0 = time.time()
    try:
        resp = await litellm.acompletion(
            model=to_litellm_model(req.model),
            messages=_to_messages(req.messages),
            temperature=req.temperature,
            max_tokens=req.max_tokens,
            api_key=req.model.api_key,
            api_base=req.model.base_url,
        )
    except Exception as exc:  # noqa: BLE001
        raise LLMError(f"LLM 调用失败: {exc}", cause=exc) from exc

    content = resp.choices[0].message.content or ""
    return LLMResponse(
        content=content,
        model=to_litellm_model(req.model),
        usage=_usage(resp),
        raw=resp,
        finish_reason=getattr(resp.choices[0], "finish_reason", None),
        latency_ms=(time.time() - t0) * 1000.0,
    )


@retry(
    retry=retry_if_exception_type((litellm.RateLimitError, litellm.Timeout, litellm.APIConnectionError)),
    wait=wait_exponential(multiplier=1, min=1, max=30),
    stop=stop_after_attempt(6),
    reraise=True,
)
async def complete_json(req: LLMRequest, schema: Type[T]) -> T:
    """结构化 JSON 补全（用 instructor 强制 schema）。"""
    t0 = time.time()
    try:
        resp = await _instructor_client.chat.completions.create(
            model=to_litellm_model(req.model),
            messages=_to_messages(req.messages),
            response_model=schema,
            temperature=req.temperature,
            max_tokens=req.max_tokens,
            api_key=req.model.api_key,
            api_base=req.model.base_url,
        )
    except Exception as exc:  # noqa: BLE001
        raise LLMError(f"LLM 结构化调用失败: {exc}", cause=exc) from exc
    return resp


async def chat(
    messages: list[LLMMessage],
    *,
    model: Optional[ModelSpec] = None,
    temperature: float = 0.7,
    max_tokens: Optional[int] = None,
) -> LLMResponse:
    """便捷入口：直接传消息列表，model 缺省时走统一解析入口。

    注：限流由 settings.llm_rate_limit_rpm 在调用方处统一管控；
    此处聚焦重试与解析，不重复内置限速器。
    """
    spec = model or get_model_spec()
    req = LLMRequest(
        model=spec,
        messages=messages,
        temperature=temperature,
        max_tokens=max_tokens,
    )
    return await complete(req)
