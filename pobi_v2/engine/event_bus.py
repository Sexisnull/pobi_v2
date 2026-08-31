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
import logging
from abc import ABC, abstractmethod
from typing import Any, Optional

logger = logging.getLogger(__name__)

from pobi_agent.hooks import EventHooks

from pobi_v2.core.config import settings
from pobi_v2.db.models import Task


def _truncate(text: Any, limit: int) -> str:
    """截断超长文本，避免事件载荷刷屏。空值安全。"""
    if text is None:
        return ""
    text = str(text)
    if len(text) <= limit:
        return text
    return text[:limit] + "…"


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
    # 事件总线/SSE 线上的信封结构：{type, session_id, payload:{...}}。
    # 注意：DB TaskEvent.payload 只存内层 payload（见 persist_event_worker 解包），
    # 消费方（/plan、/live、/events 与前端 eventToChat）均按单层读取，勿在此叠加。
    return {"type": event_type, "session_id": session_id, "payload": payload}


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

    def emit_agent_end(self, session_id, agent_name, task, confidence_score=None, task_id=None,
                       notes=None, thought_summary=None, attempts=None, role=None):
        payload = _wrap("agent_end", session_id, agent_name=agent_name, task=task,
                        confidence_score=confidence_score, task_id=task_id, notes=notes,
                        thought_summary=thought_summary, attempts=attempts,
                        role=role or "agent")
        asyncio.create_task(bus.publish(session_id, payload))
        asyncio.create_task(bus.publish("__plan_persist__", payload))

    def emit_agent_error(self, session_id, agent_name, task, error_type, error_message,
                         task_id=None, partial_reasoning=None):
        payload = _wrap("agent_error", session_id, agent_name=agent_name, task=task,
                        error_type=error_type, error_message=error_message,
                        task_id=task_id, partial_reasoning=partial_reasoning)
        asyncio.create_task(bus.publish(session_id, payload))
        asyncio.create_task(bus.publish("__plan_persist__", payload))

    def emit_agent_thought(self, session_id, agent_name, thought, summary=None):
        payload = _wrap("agent_thought", session_id, agent_name=agent_name,
                        thought=thought, summary=summary)
        asyncio.create_task(bus.publish(session_id, payload))
        asyncio.create_task(bus.publish("__plan_persist__", payload))

    def emit_agent_routed(self, session_id, task, selected_agent, reasoning, available_agents=None):
        payload = _wrap("agent_routed", session_id, task=task, selected_agent=selected_agent,
                        reasoning=reasoning, available_agents=available_agents)
        asyncio.create_task(bus.publish(session_id, payload))
        asyncio.create_task(bus.publish("__plan_persist__", payload))

    def emit_tool_call_start(self, session_id, agent_name, tool_name, args="", tool_call_id=None):
        payload = _wrap("tool_call_start", session_id, agent_name=agent_name, tool_name=tool_name,
                        args=args, tool_call_id=tool_call_id)
        asyncio.create_task(bus.publish(session_id, payload))
        asyncio.create_task(bus.publish("__plan_persist__", payload))

    def emit_tool_call_end(self, session_id, agent_name, tool_name, success, result="",
                           error=None, tool_call_id=None, duration_ms=None):
        payload = _wrap("tool_call_end", session_id, agent_name=agent_name, tool_name=tool_name,
                        success=success, result=result, error=error,
                        tool_call_id=tool_call_id, duration_ms=duration_ms)
        asyncio.create_task(bus.publish(session_id, payload))
        asyncio.create_task(bus.publish("__plan_persist__", payload))

    def emit_task_created(self, session_id, task, task_id, depth, parent_task_id=None, initial_confidence=0.0):
        payload = _wrap("task_created", session_id, task=task, task_id=task_id, depth=depth,
                        parent_task_id=parent_task_id, initial_confidence=initial_confidence)
        asyncio.create_task(bus.publish(session_id, payload))
        asyncio.create_task(bus.publish("__plan_persist__", payload))

    def emit_task_expanded(self, session_id, parent_task, parent_task_id, subtasks):
        payload = _wrap("task_expanded", session_id, parent_task=parent_task,
                        parent_task_id=parent_task_id, subtasks=subtasks)
        asyncio.create_task(bus.publish(session_id, payload))
        asyncio.create_task(bus.publish("__plan_persist__", payload))

    def emit_task_status_changed(self, session_id, task, task_id, old_status, new_status, confidence_score=None):
        payload = _wrap("task_status_changed", session_id, task=task, task_id=task_id,
                        old_status=old_status, new_status=new_status,
                        confidence_score=confidence_score)
        asyncio.create_task(bus.publish(session_id, payload))
        asyncio.create_task(bus.publish("__plan_persist__", payload))

    def emit_confidence_update(self, session_id, task, task_id, old_confidence, new_confidence, decision):
        payload = _wrap("confidence_update", session_id, task=task, task_id=task_id,
                        old_confidence=old_confidence, new_confidence=new_confidence, decision=decision)
        asyncio.create_task(bus.publish(session_id, payload))
        asyncio.create_task(bus.publish("__plan_persist__", payload))

    def emit_validation_result(self, session_id, task, task_id, valid, confidence_score, critique,
                               validation_token=None):
        payload = _wrap("validation_result", session_id, task=task, task_id=task_id, valid=valid,
                        confidence_score=confidence_score, critique=critique,
                        validation_token=validation_token)
        asyncio.create_task(bus.publish(session_id, payload))
        asyncio.create_task(bus.publish("__plan_persist__", payload))

    def emit_log_message(self, session_id, message, level="info", source=None, agent_name=None):
        payload = _wrap("log", session_id, message=message, level=level,
                        source=source, agent_name=agent_name)
        asyncio.create_task(bus.publish(session_id, payload))
        asyncio.create_task(bus.publish("__plan_persist__", payload))

    def emit_report(self, session_id, summary, title=None):
        """最终安全评估报告：推送到实时 SSE 并落库（前端聊天流以 report_task_event 渲染）。

        summary 截断至 8000 字符，避免超大报告阻塞事件循环。
        """
        payload = _wrap("report_task_event", session_id, content=_truncate(summary, 8000),
                        summary=_truncate(summary, 8000), title=title or "安全评估报告")
        asyncio.create_task(bus.publish(session_id, payload))
        asyncio.create_task(bus.publish("__plan_persist__", payload))

    def emit_plan_step(self, session_id, step_id, seq, title, status, detail=None,
                       total=None, completed=None):
        """结构化执行计划步骤：发布到事件总线（前端『执行计划』左栏消费）。

        status 取值：pending | running | completed | failed。
        total/completed 可选，提供时前端进度以该权威值为准，避免新增行污染计数。
        同时发布到 __plan_persist__ 通道，由 persist_event_worker 落库（供 /plan 聚合）。
        """
        payload = _wrap("plan_step", session_id, step_id=step_id, seq=seq,
                        title=title, status=status, detail=detail)
        if total is not None:
            payload["payload"]["total"] = total
        if completed is not None:
            payload["payload"]["completed"] = completed
        asyncio.create_task(bus.publish(session_id, payload))
        asyncio.create_task(bus.publish("__plan_persist__", payload))

    def emit_phase_changed(self, session_id, phase, detail=None):
        """阶段流转事件：发布到事件总线并持久化（供 /live 聚合当前阶段）。"""
        payload = _wrap("phase_changed", session_id, new_phase=phase, detail=detail)
        asyncio.create_task(bus.publish(session_id, payload))
        asyncio.create_task(bus.publish("__plan_persist__", payload))

    def emit_llm_iteration(self, session_id, agent_name, iteration, message_count):
        """LLM 迭代开始：推送迭代号与消息计数，前端渲染为『Iteration N』标题。"""
        payload = _wrap("llm_iteration", session_id,
                        agent_name=agent_name, iteration=iteration, message_count=message_count)
        asyncio.create_task(bus.publish(session_id, payload))
        asyncio.create_task(bus.publish("__plan_persist__", payload))

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
        wrapped = _wrap("llm_input", session_id, **payload)
        asyncio.create_task(bus.publish(session_id, wrapped))
        asyncio.create_task(bus.publish("__plan_persist__", wrapped))

    def emit_llm_response(self, session_id, agent_name, response_text, thinking_text=None,
                           usage=None):
        """LLM 响应：模型返回的完整文本（含可选 thinking/reasoning）。

        response_text 截断至 3000 字符；thinking_text 截断至 2000 字符。
        前端以可折叠面板展示，保留可观测性同时避免刷屏。

        usage（litellm/pydantic_ai 的 usage 对象，可选）用于累计会话级 token 用量，
        供 executor 在任务结束时写回 Task 的 Token 三列。
        """
        # 累计会话级 token 用量（发送=prompt / 接收=completion）
        if usage is not None:
            _accumulate_session_usage(session_id, usage)

        payload: dict[str, Any] = {
            "agent_name": agent_name,
            "response_text": _truncate(response_text, 3000),
        }
        if thinking_text:
            payload["thinking_text"] = _truncate(thinking_text, 2000)
        # 附加实时 token 累计，供前端经 SSE 增量刷新 token 卡片
        store = _session_usage_store.get(session_id)
        if store is not None:
            payload["token_usage"] = store.as_dict()
        wrapped = _wrap("llm_response", session_id, **payload)
        asyncio.create_task(bus.publish(session_id, wrapped))
        asyncio.create_task(bus.publish("__plan_persist__", wrapped))

    def is_interrupted(self, session_id: str) -> bool:
        # M3：协作式取消——查询 cancel_state 的中断标志
        from pobi_v2.engine.cancel_state import is_cancelled_sync

        return is_cancelled_sync(session_id)

    # ──────────────────────────────────────────────────────────────────────
    # RECON 扩展事件（非 EventHooks Protocol 成员，仅 PobiV2EventHooks 提供）
    # 供 ContextEngine 旁路写入本地库后，触发 PG 聚合层异步增量同步。
    #
    # ⚠️ 弃用（2026-08-27）：原经内存事件总线推送 __recon_sync__，由 web 进程的
    # recon_sync_worker 消费。但 agent 运行于 worker 进程、默认内存总线不跨进程，
    # 事件丢失导致 PG 聚合层空表。现改为 ContextEngine 同进程直连 upsert_to_pg +
    # deadend_runner finally 兜底 flush（见 context_engine.py / deadend_runner.py）。
    # 本方法保留仅作兼容空壳，不再被任何调用方使用。
    # ──────────────────────────────────────────────────────────────────────
    def emit_recon_upsert(
        self,
        session_id: str,
        target_id: str,
        tenant_id: str,
        task_id: str,
        source: str = "context_engine",
    ) -> None:
        """[弃用] 见类上方注释。保留空壳以避免潜在引用报错；新同步路径不依赖事件总线。"""
        return
        payload = _wrap(
            "recon_upsert",
            session_id,
            target_id=target_id,
            tenant_id=tenant_id,
            task_id=task_id,
            source=source,
        )
        asyncio.create_task(bus.publish("__recon_sync__", payload))


async def persist_event_worker() -> None:
    """后台持久化：把总线上的控制台事件写入 TaskEvent 表（供 /plan 与 /live 聚合）。

    持久化类型包括 plan_step（执行计划）、phase_changed（当前阶段）、
    agent_start/agent_end（运行视图）。其他事件由 SSE 实时消费，无需落库。
    """
    from pobi_v2.db.session import AsyncSessionLocal
    from pobi_v2.db.persistence import record_task_event, _utcnow

    persist_event_types = {
        # 原有骨架（计划/阶段/运行视图）
        "plan_step",
        "phase_changed",
        "agent_start",
        "agent_end",
        "tool_call_start",
        "tool_call_end",
        "report_task_event",
        # 新增明细（agent 思考与 LLM 过程，补全 API 不可见缺口）
        "agent_thought",
        "agent_error",
        "agent_routed",
        "llm_iteration",
        "llm_input",
        "llm_response",
        "confidence_update",
        "validation_result",
        "task_created",
        "task_expanded",
        "task_status_changed",
        "log",
    }
    queue = await bus.subscribe("__plan_persist__")
    # 节流：每落库 _TOUCH_EVERY 条事件刷新一次任务的 updated_at，
    # 避免高频事件下每条都写库，同时防止长时间任务在 API 侧 updated_at 冻结。
    _TOUCH_EVERY = 10
    since_touch = 0
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

            from sqlalchemy import update

            task_uuid = _UUID(str(task_id))
            async with AsyncSessionLocal() as session:
                # 落库只存内层 payload（去掉 _wrap 的 {type, session_id, payload} 外壳），
                # 与 executor 直接落库的 agent_result 及 /plan、/live、/events 消费方
                # 的单层口径保持一致；否则双层嵌套会导致回放时 iteration/content 等字段解析不到。
                detail = event.get("payload") or {}
                await record_task_event(
                    session,
                    task_uuid,
                    event_type,
                    detail,
                )
                since_touch += 1
                if since_touch >= _TOUCH_EVERY:
                    since_touch = 0
                    await session.execute(
                        update(Task)
                        .where(Task.id == task_uuid)
                        .values(updated_at=_utcnow())
                    )
                await session.commit()
        except Exception:  # noqa: BLE001
            # 持久化失败不影响主流程与实时推送
            continue


async def recon_sync_worker() -> None:
    """[弃用] RECON 本地库 → PG 聚合层同步 worker（设计文档 §5，第二阶）。

    ⚠️ 弃用（2026-08-27）：原经内存事件总线订阅 ``__recon_sync__``，但 agent 运行
    于 worker 进程、默认内存总线不跨进程，web 进程订阅方永远收不到事件，导致 PG
    聚合层空表。现改为 ContextEngine 同进程直连 upsert_to_pg + deadend_runner
    finally 兜底 flush。本函数保留为空壳，main.py 已停止启动它。
    """
    return
    from pobi_agent.recon import ReconStore
    from pobi_v2.db.session import AsyncSessionLocal

    queue = await bus.subscribe("__recon_sync__")
    while True:
        try:
            event = await queue.get()
        except Exception:  # noqa: BLE001
            continue
        if event.get("type") != "recon_upsert":
            continue
        session_id = event.get("session_id")
        target_id = event.get("target_id")
        tenant_id = event.get("tenant_id")
        task_id = event.get("task_id")
        if not (session_id and target_id and tenant_id):
            continue
        try:
            # 定位本地库：经 storage_context 取 task_root。
            from pobi_agent.storage_context import get_task_root

            task_root = get_task_root()
            if task_root is None:
                continue
            db_path = task_root / f"{session_id}.db"
            if not db_path.exists():
                continue
            store = ReconStore(db_path)
            await store.upsert_to_pg(
                target_id=target_id,
                tenant_id=tenant_id,
                task_id=task_id,
                async_session_factory=AsyncSessionLocal,
            )
            store.close()
        except Exception as exc:  # noqa: BLE001
            logger.warning("RECON PG 同步失败（已忽略）: %s", exc)


# ──────────────────────────────────────────────────────────────────────────
# 会话级 Token 用量累计（供 Token 用量页 / 任务落库）
# ──────────────────────────────────────────────────────────────────────────
class _SessionUsage:
    """单个会话的累计 token 用量。prompt=发送，completion=接收。"""

    __slots__ = ("prompt_tokens", "completion_tokens", "total_tokens")

    def __init__(self) -> None:
        self.prompt_tokens: int = 0
        self.completion_tokens: int = 0
        self.total_tokens: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
        }


_session_usage_store: dict[str, _SessionUsage] = {}
_session_usage_lock = asyncio.Lock()


def _usage_redis_key(session_id: str) -> str:
    """Redis 中会话级 token 实时累计的 key。"""
    return f"pobi:usage:{session_id}"


def _push_usage_to_redis(session_id: str, usage: Any) -> None:
    """把单次 LLM usage 异步累加到 Redis（跨进程实时真源，供 api/SSE 读取）。

    事件总线为 Redis 后端时生效；memory 后端（开发模式）跳过。
    采用 HINCRBY 原子累加，多 worker 副本下各副本写入可正确合并。
    """
    redis_client = getattr(bus, "_redis", None)
    if redis_client is None:
        return
    prompt = int(getattr(usage, "prompt_tokens", 0) or 0)
    completion = int(getattr(usage, "completion_tokens", 0) or 0)
    total = int(getattr(usage, "total_tokens", 0) or 0) or (prompt + completion)
    if not (prompt or completion or total):
        return

    async def _incr() -> None:
        try:
            key = _usage_redis_key(session_id)
            pipe = redis_client.pipeline()
            pipe.hincrby(key, "prompt_tokens", prompt)
            pipe.hincrby(key, "completion_tokens", completion)
            pipe.hincrby(key, "total_tokens", total)
            pipe.expire(key, 86400)  # 24h 兜底，避免孤儿 key 长期残留
            await pipe.execute()
        except Exception:
            pass

    try:
        asyncio.create_task(_incr())
    except Exception:
        pass


def _accumulate_session_usage(session_id: str, usage: Any) -> None:
    """把单次 LLM 响应的 usage 累加到会话计数器（线程/协程安全，O(1)）。

    内存累计供 executor 任务结束落库；同时异步写入 Redis 供 api 进程
    实时读取（任务详情页 token 卡片 / SSE 增量推送）。
    """
    prompt = int(getattr(usage, "prompt_tokens", 0) or 0)
    completion = int(getattr(usage, "completion_tokens", 0) or 0)
    total = int(getattr(usage, "total_tokens", 0) or 0) or (prompt + completion)
    store = _session_usage_store.setdefault(session_id, _SessionUsage())
    store.prompt_tokens += prompt
    store.completion_tokens += completion
    store.total_tokens += total
    _push_usage_to_redis(session_id, usage)


async def get_session_usage(session_id: str) -> dict[str, int]:
    """读取会话累计 token 用量（内存，executor 落库用）。"""
    async with _session_usage_lock:
        store = _session_usage_store.get(session_id)
        return store.as_dict() if store else _SessionUsage().as_dict()


async def get_realtime_usage(session_id: str) -> dict[str, int]:
    """读取会话实时 token 累计（api 进程侧）。

    优先 Redis 跨进程真源（多 worker 合并后的累计）；无实时数据时回退
    内存计数（同一进程内等价）。Redis 不可用或 key 不存在时返回全 0。
    """
    redis_client = getattr(bus, "_redis", None)
    if redis_client is not None:
        try:
            data = await redis_client.hgetall(_usage_redis_key(session_id))
            if data:
                return {
                    "prompt_tokens": int(data.get("prompt_tokens", 0) or 0),
                    "completion_tokens": int(data.get("completion_tokens", 0) or 0),
                    "total_tokens": int(data.get("total_tokens", 0) or 0),
                }
        except Exception:
            pass
    return await get_session_usage(session_id)


async def reset_session_usage(session_id: str) -> None:
    """清空会话累计（任务启动时调用，避免跨任务污染）。"""
    async with _session_usage_lock:
        _session_usage_store.pop(session_id, None)
    redis_client = getattr(bus, "_redis", None)
    if redis_client is not None:
        try:
            await redis_client.delete(_usage_redis_key(session_id))
        except Exception:
            pass
