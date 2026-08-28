"""平台统一 LLM 抽象层：litellm + instructor + tenacity。

这是 pobi_v2 平台解析与调用 LLM 的**唯一入口**：所有执行路径（agent 内核
``CoreAgent`` / ``DeadEndAgent``、平台自包含问答/报告/probe）都必须经本层
``complete`` / ``complete_json`` / ``chat`` 发起 LLM 调用，禁止在别处直接
``litellm.acompletion`` / ``instructor``。

职责：
- 统一模型解析（``to_litellm_model``）、限流与重试（tenacity）；
- 支持 function calling（``LLMRequest.tools``）与扩展推理（``thinking_content``）；
- 异常统一归一为内核 ``pobi_agent.core_agent`` 异常体系，重试作用于归一后的
  可重试类型（RateLimit / Connection），上层按内核异常捕获即可；
- 返回结构化 ``LLMResponse``（含 usage / tool_calls / thinking / raw）。
"""

from __future__ import annotations

import asyncio
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
# 统一异常体系：所有 LLM 失败归一为内核 core_agent 异常（平台侧宽泛 except 兼容）
from pobi_agent.core_agent import (
    AuthenticationError as CoreAuthenticationError,
    ConnectionError as CoreConnectionError,
    InvalidRequestError as CoreInvalidRequestError,
    LLMError as CoreLLMError,
    ModelNotFoundError as CoreModelNotFoundError,
    QuotaExceededError as CoreQuotaExceededError,
    RateLimitError as CoreRateLimitError,
)
from pobi_v2.core.config import settings
from pobi_v2.llm.config import get_model_spec, to_litellm_model
from pobi_v2.llm.types import (
    LLMMessage,
    LLMRequest,
    LLMResponse,
    Role,
    UsageRecord,
)

T = TypeVar("T", bound=BaseModel)

_instructor_client = from_litellm(litellm.acompletion)

# 进程级 LLM 并发信号量：多任务并行时所有 LLM 调用经此统一卡口，
# 避免 QPS 线性放大触发上游 RateLimitError（429）雪崩。与 task_id 无关——
# 因为 litellm.acompletion 本身是无状态自包含 HTTP 调用（请求/响应天然隔离），
# 不存在结果串台，冲突只来自上游限速，故限全局并发而非逐任务绑定。
_llm_semaphore = asyncio.Semaphore(max(1, settings.llm_max_concurrency))

# 归一后可重试的异常类型：驱动 tenacity 在分类后的异常上重试
_RETRYABLE = (CoreRateLimitError, CoreConnectionError)


def _to_messages(msgs: list[Any]) -> list[dict]:
    """把 ``LLMMessage`` 或 OpenAI 原生 dict 消息统一转为 litellm 消息列表。

    原生 dict 消息（含 ``tool_calls`` / ``tool`` 角色 / ``thinking_content`` 等
    扩展字段）原样透传，保证 agent 完整对话历史不丢失。
    """
    out = []
    for m in msgs:
        if isinstance(m, dict):
            out.append(m)
            continue
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


def _extract_thinking(message: Any) -> str:
    """提取扩展推理模型的思考内容（litellm 的 ``reasoning_content``）。"""
    try:
        return getattr(message, "reasoning_content", None) or ""
    except Exception:
        return ""


def _extract_tool_calls(message: Any) -> Optional[list[dict]]:
    """把 litellm tool_calls 提取为 OpenAI 兼容 dict 列表。"""
    tcs = getattr(message, "tool_calls", None)
    if not tcs:
        return None
    return [
        {
            "id": tc.id,
            "type": "function",
            "function": {
                "name": tc.function.name,
                "arguments": tc.function.arguments,
            },
        }
        for tc in tcs
    ]


def _normalize_error(exc: Exception) -> Exception:
    """把 litellm / instructor 异常归一为内核 core_agent 异常体系。

    分类规则与内核原有字符串匹配一致但更精确（按异常类型），使上层统一按内核
    异常捕获。重试型（RateLimit / Connection）由 tenacity 继续作用。
    """
    msg = str(exc)
    lower = msg.lower()

    # 内容政策违规：携带 provider 细节，归一为 InvalidRequestError（不可重试）
    if isinstance(exc, litellm.exceptions.ContentPolicyViolationError):
        details = getattr(exc, "provider_specific_fields", None) or {}
        detail_str = f" provider={details}" if details else ""
        return CoreInvalidRequestError(
            f"Request blocked by provider content policy.{detail_str} {msg}",
            original_error=exc,
        )

    # 配额耗尽（billing，不可重试）
    if "insufficient_quota" in lower or "exceeded your current quota" in lower:
        return CoreQuotaExceededError(f"API quota exceeded. {msg}", original_error=exc)

    # 速率限制（可重试）
    if isinstance(exc, litellm.exceptions.RateLimitError):
        return CoreRateLimitError(f"Rate limit exceeded. {msg}", original_error=exc)

    # 鉴权失败
    if isinstance(exc, litellm.exceptions.AuthenticationError):
        return CoreAuthenticationError(
            f"API authentication failed. {msg}", original_error=exc
        )

    # 模型不存在
    if isinstance(exc, litellm.exceptions.NotFoundError) or (
        "model" in lower and ("not found" in lower or "does not exist" in lower)
    ):
        return CoreModelNotFoundError(f"Model not found. {msg}", original_error=exc)

    # 连接 / 超时 / 服务不可用（可重试）
    if isinstance(
        exc,
        (
            litellm.exceptions.APIConnectionError,
            litellm.exceptions.Timeout,
            litellm.exceptions.ServiceUnavailableError,
        ),
    ):
        return CoreConnectionError(
            f"Failed to connect to the API. {msg}", original_error=exc
        )

    # 无效请求
    if isinstance(exc, litellm.exceptions.BadRequestError):
        return CoreInvalidRequestError(
            f"Invalid request to the API: {msg}", original_error=exc
        )

    return CoreLLMError(f"LLM request failed: {msg}", original_error=exc)


@retry(
    retry=retry_if_exception_type(_RETRYABLE),
    wait=wait_exponential(multiplier=1, min=1, max=30),
    stop=stop_after_attempt(6),
    reraise=True,
)
async def complete(req: LLMRequest) -> LLMResponse:
    """文本补全（统一入口，带限流与重试，支持 tools / json_mode / 思考内容）。"""
    t0 = time.time()
    try:
        kwargs: dict[str, Any] = {
            "model": to_litellm_model(req.model),
            "messages": _to_messages(req.messages),
            "temperature": req.temperature,
            "api_key": req.model.api_key,
            "api_base": req.model.base_url,
        }
        if req.max_tokens:
            kwargs["max_tokens"] = req.max_tokens
        if req.tools:
            kwargs["tools"] = req.tools
        if req.json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        resp = await litellm.acompletion(**kwargs)
    except Exception as exc:  # noqa: BLE001
        raise _normalize_error(exc) from exc

    choice = resp.choices[0]
    message = choice.message
    return LLMResponse(
        content=message.content or "",
        model=to_litellm_model(req.model),
        usage=_usage(resp),
        raw=resp,
        finish_reason=getattr(choice, "finish_reason", None),
        latency_ms=(time.time() - t0) * 1000.0,
        thinking_content=_extract_thinking(message),
        tool_calls=_extract_tool_calls(message),
    )


@retry(
    retry=retry_if_exception_type(_RETRYABLE),
    wait=wait_exponential(multiplier=1, min=1, max=30),
    stop=stop_after_attempt(6),
    reraise=True,
)
async def complete_json(req: LLMRequest, schema: Type[T]) -> T:
    """结构化 JSON 补全（统一入口，用 instructor 强制 schema）。"""
    t0 = time.time()
    # 全局并发信号量：与 complete 共享同一卡口，统一限流。
    async with _llm_semaphore:
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
            raise _normalize_error(exc) from exc
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
