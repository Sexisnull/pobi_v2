"""事件总线：对接 pobi_agent 的 EventHooks，为 M2 的 SSE 实时推送提供通道。

pobi_agent.CoreAgent / DeadEndAgent 通过全局 `get_event_hooks()` 推送事件
（见 pobi_agent/hooks.py 的 EventHooks Protocol）。我们实现一个兼容该 Protocol
的对象，把事件按 session_id 发布到总线；SSE 端点订阅该通道，向前端实时推送。

支持两种后端：
- memory：进程内 asyncio.Queue（开发、单 worker）。
- redis：Redis pub/sub（生产、多 worker，跨进程分发）。
"""
from __future__ import annotations

import asyncio
import json
from abc import ABC, abstractmethod
from typing import Any, Optional

from pobi_agent.hooks import EventHooks

from pobi_v2.core.config import settings


class EventBusBackend(ABC):
    @abstractmethod
    async def subscribe(self, session_id: str) -> asyncio.Queue: ...

    @abstractmethod
    async def unsubscribe(self, session_id: str, queue: asyncio.Queue) -> None: ...

    @abstractmethod
    async def publish(self, session_id: str, event: dict[str, Any]) -> None: ...


class MemoryEventBusBackend(EventBusBackend):
    def __init__(self) -> None:
        self._subscribers: dict[str, set[asyncio.Queue]] = {}
        self._lock = asyncio.Lock()

    async def subscribe(self, session_id: str) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue()
        async with self._lock:
            self._subscribers.setdefault(session_id, set()).add(queue)
        return queue

    async def unsubscribe(self, session_id: str, queue: asyncio.Queue) -> None:
        async with self._lock:
            subs = self._subscribers.get(session_id)
            if subs:
                subs.discard(queue)
                if not subs:
                    self._subscribers.pop(session_id, None)

    async def publish(self, session_id: str, event: dict[str, Any]) -> None:
        async with self._lock:
            subs = list(self._subscribers.get(session_id, set()))
        for q in subs:
            await q.put(event)


class RedisEventBusBackend(EventBusBackend):
    """基于 Redis pub/sub 的事件总线（跨进程分发，适配多 Worker）。"""

    def __init__(self, redis_url: str) -> None:
        import redis.asyncio as aioredis

        self._redis = aioredis.from_url(redis_url, decode_responses=True)
        self._local: dict[str, set[asyncio.Queue]] = {}
        self._lock = asyncio.Lock()

    def _channel(self, session_id: str) -> str:
        return f"pobi_v2:events:{session_id}"

    async def subscribe(self, session_id: str) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue()
        pubsub = self._redis.pubsub()
        await pubsub.subscribe(self._channel(session_id))

        async def _reader() -> None:
            async for message in pubsub.listen():
                if message and message.get("type") == "message":
                    try:
                        evt = json.loads(message["data"])
                        await queue.put(evt)
                    except (json.JSONDecodeError, TypeError):
                        continue

        async with self._lock:
            self._local.setdefault(session_id, set()).add(queue)
        asyncio.create_task(_reader())
        return queue

    async def unsubscribe(self, session_id: str, queue: asyncio.Queue) -> None:
        async with self._lock:
            self._local.get(session_id, set()).discard(queue)

    async def publish(self, session_id: str, event: dict[str, Any]) -> None:
        await self._redis.publish(self._channel(session_id), json.dumps(event, default=str))


def _build_backend() -> EventBusBackend:
    if settings.event_bus_backend == "redis":
        return RedisEventBusBackend(settings.redis_url)
    return MemoryEventBusBackend()


bus: EventBusBackend = _build_backend()


def _wrap(event_type: str, session_id: str, **payload: Any) -> dict[str, Any]:
    return {"type": event_type, "session_id": session_id, **payload}


class PobiV2EventHooks:
    """将 pobi_agent 的事件转发到事件总线。

    实现 hooks.EventHooks Protocol 的全部方法（结构化子类型，方法名/签名需匹配）。
    """

    def emit_agent_start(self, session_id, agent_name, task, task_id=None, depth=0,
                         parent_task_id=None, role=None):
        payload = _wrap("agent_start", session_id, agent_name=agent_name, task=task,
                        task_id=task_id, depth=depth, parent_task_id=parent_task_id,
                        role=role or "agent")
        asyncio.create_task(bus.publish(session_id, payload))
        asyncio.create_task(bus.publish("__plan_persist__", payload))

    def emit_agent_end(self, session_id, agent_name, task, confidence_score, task_id=None,
                       notes=None, thought_summary=None, attempts=None, role=None):
        payload = _wrap("agent_end", session_id, agent_name=agent_name, task=task,
                        confidence_score=confidence_score, task_id=task_id, notes=notes,
                        thought_summary=thought_summary, attempts=attempts,
                        role=role or "agent")
        asyncio.create_task(bus.publish(session_id, payload))
        asyncio.create_task(bus.publish("__plan_persist__", payload))

    def emit_agent_error(self, session_id, agent_name, task, error_type, error_message,
                         task_id=None, partial_reasoning=None):
        asyncio.create_task(
            bus.publish(
                session_id,
                _wrap("agent_error", session_id, agent_name=agent_name, task=task,
                      error_type=error_type, error_message=error_message,
                      task_id=task_id, partial_reasoning=partial_reasoning),
            )
        )

    def emit_agent_thought(self, session_id, agent_name, thought, summary=None):
        asyncio.create_task(
            bus.publish(
                session_id,
                _wrap("agent_thought", session_id, agent_name=agent_name,
                      thought=thought, summary=summary),
            )
        )

    def emit_agent_routed(self, session_id, task, selected_agent, reasoning, available_agents=None):
        asyncio.create_task(
            bus.publish(
                session_id,
                _wrap("agent_routed", session_id, task=task, selected_agent=selected_agent,
                      reasoning=reasoning, available_agents=available_agents),
            )
        )

    def emit_tool_call_start(self, session_id, agent_name, tool_name, args="", tool_call_id=None):
        asyncio.create_task(
            bus.publish(
                session_id,
                _wrap("tool_call_start", session_id, agent_name=agent_name, tool_name=tool_name,
                      args=args, tool_call_id=tool_call_id),
            )
        )

    def emit_tool_call_end(self, session_id, agent_name, tool_name, success, result="",
                           error=None, tool_call_id=None, duration_ms=None):
        asyncio.create_task(
            bus.publish(
                session_id,
                _wrap("tool_call_end", session_id, agent_name=agent_name, tool_name=tool_name,
                      success=success, result=result, error=error,
                      tool_call_id=tool_call_id, duration_ms=duration_ms),
            )
        )

    def emit_task_created(self, session_id, task, task_id, depth, parent_task_id=None, initial_confidence=0.0):
        asyncio.create_task(
            bus.publish(
                session_id,
                _wrap("task_created", session_id, task=task, task_id=task_id, depth=depth,
                      parent_task_id=parent_task_id, initial_confidence=initial_confidence),
            )
        )

    def emit_task_expanded(self, session_id, parent_task, parent_task_id, subtasks):
        asyncio.create_task(
            bus.publish(
                session_id,
                _wrap("task_expanded", session_id, parent_task=parent_task,
                      parent_task_id=parent_task_id, subtasks=subtasks),
            )
        )

    def emit_task_status_changed(self, session_id, task, task_id, old_status, new_status, confidence_score=None):
        asyncio.create_task(
            bus.publish(
                session_id,
                _wrap("task_status_changed", session_id, task=task, task_id=task_id,
                      old_status=old_status, new_status=new_status,
                      confidence_score=confidence_score),
            )
        )

    def emit_confidence_update(self, session_id, task, task_id, old_confidence, new_confidence, decision):
        asyncio.create_task(
            bus.publish(
                session_id,
                _wrap("confidence_update", session_id, task=task, task_id=task_id,
                      old_confidence=old_confidence, new_confidence=new_confidence, decision=decision),
            )
        )

    def emit_validation_result(self, session_id, task, task_id, valid, confidence_score, critique,
                               validation_token=None):
        asyncio.create_task(
            bus.publish(
                session_id,
                _wrap("validation_result", session_id, task=task, task_id=task_id, valid=valid,
                      confidence_score=confidence_score, critique=critique,
                      validation_token=validation_token),
            )
        )

    def emit_log_message(self, session_id, message, level="info", source=None, agent_name=None):
        asyncio.create_task(
            bus.publish(
                session_id,
                _wrap("log", session_id, message=message, level=level,
                      source=source, agent_name=agent_name),
            )
        )

    def emit_plan_step(self, session_id, step_id, seq, title, status, detail=None):
        """结构化执行计划步骤：发布到事件总线（前端『执行计划』左栏消费）。

        status 取值：pending | running | completed | failed。
        同时发布到 __plan_persist__ 通道，由 persist_event_worker 落库（供 /plan 聚合）。
        """
        payload = _wrap("plan_step", session_id, step_id=step_id, seq=seq,
                        title=title, status=status, detail=detail)
        asyncio.create_task(bus.publish(session_id, payload))
        asyncio.create_task(bus.publish("__plan_persist__", payload))

    def emit_phase_changed(self, session_id, phase, detail=None):
        """阶段流转事件：发布到事件总线并持久化（供 /live 聚合当前阶段）。"""
        payload = _wrap("phase_changed", session_id, new_phase=phase, detail=detail)
        asyncio.create_task(bus.publish(session_id, payload))
        asyncio.create_task(bus.publish("__plan_persist__", payload))

    def emit_llm_iteration(self, session_id, agent_name, iteration, message_count):
        """LLM 迭代开始：推送迭代号与消息计数，前端渲染为『Iteration N』标题。"""
        asyncio.create_task(
            bus.publish(
                session_id,
                _wrap("llm_iteration", session_id,
                      agent_name=agent_name, iteration=iteration, message_count=message_count),
            )
        )

    def emit_llm_input(self, session_id, agent_name, role, content, tool_name=None):
        """LLM 输入：本轮发给模型的最后一条消息（user prompt 或 tool result）。

        content 截断至 2000 字符，防止 SSE 消息过大阻塞事件循环。
        """
        payload: dict[str, Any] = {
            "agent_name": agent_name,
            "role": role,
            "content": _truncate(content, 2000),
        }
        if tool_name:
            payload["tool_name"] = tool_name
        asyncio.create_task(
            bus.publish(session_id, _wrap("llm_input", session_id, **payload))
        )

    def emit_llm_response(self, session_id, agent_name, response_text, thinking_text=None):
        """LLM 响应：模型返回的完整文本（含可选 thinking/reasoning）。

        response_text 截断至 3000 字符；thinking_text 截断至 2000 字符。
        前端以可折叠面板展示，保留可观测性同时避免刷屏。
        """
        payload: dict[str, Any] = {
            "agent_name": agent_name,
            "response_text": _truncate(response_text, 3000),
        }
        if thinking_text:
            payload["thinking_text"] = _truncate(thinking_text, 2000)
        asyncio.create_task(
            bus.publish(session_id, _wrap("llm_response", session_id, **payload))
        )

    def is_interrupted(self, session_id: str) -> bool:
        # M3：协作式取消——查询 cancel_state 的中断标志
        from pobi_v2.engine.cancel_state import is_cancelled_sync

        return is_cancelled_sync(session_id)


async def persist_event_worker() -> None:
    """后台持久化：把总线上的控制台事件写入 TaskEvent 表（供 /plan 与 /live 聚合）。

    持久化类型包括 plan_step（执行计划）、phase_changed（当前阶段）、
    agent_start/agent_end（运行视图）。其他事件由 SSE 实时消费，无需落库。
    """
    from pobi_v2.db.session import AsyncSessionLocal
    from pobi_v2.db.persistence import record_task_event

    persist_event_types = {"plan_step", "phase_changed", "agent_start", "agent_end"}
    queue = await bus.subscribe("__plan_persist__")
    while True:
        try:
            event = await queue.get()
        except Exception:  # noqa: BLE001
            continue
        event_type = event.get("type")
        if event_type not in persist_event_types:
            continue
        task_id = event.get("session_id")
        if not task_id:
            continue
        try:
            from uuid import UUID as _UUID

            task_uuid = _UUID(str(task_id))
            async with AsyncSessionLocal() as session:
                detail = dict(event)
                detail.pop("type", None)
                detail.pop("session_id", None)
                await record_task_event(
                    session,
                    task_uuid,
                    event_type,
                    detail,
                )
                await session.commit()
        except Exception:  # noqa: BLE001
            # 持久化失败不影响主流程与实时推送
            continue
