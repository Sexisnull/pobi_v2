"""LLM 平台调用层的对外数据类型。

注意：模型规格 ``ModelSpec`` 已统一复用内核 ``pobi_agent.config.settings.ModelSpec``
（pydantic），本模块不再自建 ModelSpec，避免类型壁垒。此处仅保留平台自包含
调用（complete/complete_json/chat）所需的消息/请求/响应/用量/错误类型。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

from pobi_agent.config.settings import ModelSpec  # 统一复用内核 ModelSpec


class Role(str, Enum):
    """消息角色。"""

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"
    FUNCTION = "function"


@dataclass
class LLMMessage:
    """单条对话消息。"""

    role: str
    content: str
    name: Optional[str] = None


@dataclass
class LLMRequest:
    """一次 LLM 调用的完整请求。"""

    model: ModelSpec
    messages: list[LLMMessage]
    temperature: float = 0.7
    max_tokens: Optional[int] = None
    json_mode: bool = False


@dataclass
class UsageRecord:
    """token 用量记录。"""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


@dataclass
class LLMResponse:
    """LLM 调用的结构化响应。"""

    content: str
    model: str
    usage: UsageRecord = field(default_factory=UsageRecord)
    raw: Any = None
    finish_reason: Optional[str] = None
    latency_ms: float = 0.0


class LLMError(RuntimeError):
    """LLM 调用失败（网络/限流/解析）。"""

    def __init__(self, message: str, *, cause: Optional[BaseException] = None):
        super().__init__(message)
        self.cause = cause


def _now_ms() -> float:  # pragma: no cover - 时间辅助
    return time.time() * 1000.0
