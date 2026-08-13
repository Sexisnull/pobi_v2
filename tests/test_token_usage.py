"""Token 用量采集与落库链路测试。

覆盖：
1. event_bus 会话级 token 累计 / 读取 / 清空。
2. emit_llm_response 携带 usage 时正确累加（发送=prompt / 接收=completion）。
3. UsageSummary / TaskUsage schema 字段完整性。
"""
from __future__ import annotations

import asyncio

import pytest

from pobi_v2.engine import event_bus
from pobi_v2.schemas.task import TaskUsage, UsageSummary


class _FakeUsage:
    """模拟 litellm / pydantic_ai 的 usage 对象。"""

    def __init__(self, prompt_tokens: int, completion_tokens: int, total_tokens: int):
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.total_tokens = total_tokens


async def test_session_usage_accumulate_and_read():
    sid = "sess-acc-1"
    await event_bus.reset_session_usage(sid)
    event_bus._accumulate_session_usage(
        sid, _FakeUsage(100, 50, 150)
    )
    event_bus._accumulate_session_usage(
        sid, _FakeUsage(200, 80, 280)
    )
    usage = await event_bus.get_session_usage(sid)
    assert usage["prompt_tokens"] == 300
    assert usage["completion_tokens"] == 130
    assert usage["total_tokens"] == 430


async def test_session_usage_empty_when_absent():
    usage = await event_bus.get_session_usage("never-started")
    assert usage == {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
    }


async def test_session_usage_reset():
    sid = "sess-reset-1"
    event_bus._accumulate_session_usage(sid, _FakeUsage(10, 20, 30))
    await event_bus.reset_session_usage(sid)
    usage = await event_bus.get_session_usage(sid)
    assert usage["prompt_tokens"] == 0
    assert usage["completion_tokens"] == 0
    assert usage["total_tokens"] == 0


async def test_emit_llm_response_accumulates_usage():
    sid = "sess-emit-1"
    await event_bus.reset_session_usage(sid)

    class _Hooks(event_bus.PobiV2EventHooks):
        pass

    hooks = _Hooks()
    # emit 内部会调用 asyncio.create_task 发布到 bus，这里只验证累加副作用
    hooks.emit_llm_response(
        session_id=sid,
        agent_name="agent",
        response_text="hello",
        usage=_FakeUsage(40, 10, 50),
    )
    # 累加是同步的（_accumulate_session_usage 内联），可直接读取
    usage = await event_bus.get_session_usage(sid)
    assert usage["prompt_tokens"] == 40
    assert usage["completion_tokens"] == 10
    assert usage["total_tokens"] == 50


def test_usage_summary_schema_defaults():
    s = UsageSummary()
    assert s.task_count == 0
    assert s.total_tokens == 0
    assert s.completed_total_tokens == 0


def test_task_usage_schema_from_attributes():
    u = TaskUsage(
        task_id="t1",
        name="scan A",
        status="completed",
        model="openai/gpt-4o",
        prompt_tokens=123,
        completion_tokens=45,
        total_tokens=168,
    )
    assert u.task_id == "t1"
    assert u.prompt_tokens == 123
    assert u.completion_tokens == 45
    assert u.total_tokens == 168
