"""运行指令通道（任务控制台：用户追加指令干预运行中的 Agent）。

与 ``cancel_state`` 同构：
- memory：进程内 dict[list]（单 worker / 开发），同步实现。
- redis：Redis list（多 worker，跨进程），异步实现。

前端通过 ``POST /api/v1/tasks/{id}/instructions`` 写入 pending 指令；
Worker 在 ``run_exploitation`` 的协作式检查点轮询并消费，把指令注入
Supervisor 上下文（见 ``pobi_agent/pobi_agent.py`` 的 ``run_exploitation``）。

对外暴露的 queue_instruction / drain_instructions / peek_instructions 统一为
async；memory 后端内部用同步 list，redis 后端用异步客户端。
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from pobi_v2.core.config import settings


@dataclass
class PendingInstruction:
    """一条待执行指令。"""

    instruction: str
    created_at: str
    meta: dict[str, Any]


class _MemoryInstructionStore:
    def __init__(self) -> None:
        # task_id -> list[PendingInstruction]
        self._queues: dict[str, list[PendingInstruction]] = {}

    def push(self, task_id, item: PendingInstruction) -> None:
        self._queues.setdefault(str(task_id), []).append(item)

    def pop_all(self, task_id) -> list[PendingInstruction]:
        q = self._queues.get(str(task_id))
        if not q:
            return []
        self._queues[str(task_id)] = []
        return q

    def peek(self, task_id) -> list[PendingInstruction]:
        return list(self._queues.get(str(task_id), []))


class _RedisInstructionStore:
    def __init__(self, redis_url: str) -> None:
        import redis.asyncio as aioredis

        self._redis = aioredis.from_url(redis_url, decode_responses=True)
        self._key_prefix = "pobi_v2:instructions:"

    def _key(self, task_id) -> str:
        return f"{self._key_prefix}{task_id}"

    async def push(self, task_id, item: PendingInstruction) -> None:
        payload = _serialize(item)
        await self._redis.rpush(self._key(task_id), payload)

    async def pop_all(self, task_id) -> list[PendingInstruction]:
        key = self._key(task_id)
        raws = await self._redis.lrange(key, 0, -1)
        await self._redis.delete(key)
        return [_deserialize(r) for r in raws]

    async def peek(self, task_id) -> list[PendingInstruction]:
        raws = await self._redis.lrange(self._key(task_id), 0, -1)
        return [_deserialize(r) for r in raws]


_store: object | None = None


def _get_store():
    global _store
    if _store is not None:
        return _store
    if settings.event_bus_backend == "redis":
        _store = _RedisInstructionStore(settings.redis_url)
    else:
        _store = _MemoryInstructionStore()
    return _store


def _serialize(item: PendingInstruction) -> str:
    import json

    return json.dumps(
        {
            "instruction": item.instruction,
            "created_at": item.created_at,
            "meta": item.meta,
        },
        ensure_ascii=False,
    )


def _deserialize(raw: str) -> PendingInstruction:
    import json

    data = json.loads(raw)
    return PendingInstruction(
        instruction=data["instruction"],
        created_at=data.get("created_at", ""),
        meta=data.get("meta", {}),
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def queue_instruction(task_id, instruction: str, meta: dict[str, Any] | None = None) -> None:
    """写入一条待执行指令。失败安全：redis 异常时静默降级（不影响主流程）。"""
    item = PendingInstruction(
        instruction=instruction,
        created_at=_now(),
        meta=meta or {},
    )
    store = _get_store()
    try:
        if isinstance(store, _MemoryInstructionStore):
            store.push(task_id, item)
        else:
            await store.push(task_id, item)
    except Exception:  # noqa: BLE001
        # 指令通道异常不应阻断主任务；记录交由调用方。
        return


async def drain_instructions(task_id) -> list[PendingInstruction]:
    """取出并清空该任务的所有 pending 指令（Worker 检查点调用）。"""
    store = _get_store()
    try:
        if isinstance(store, _MemoryInstructionStore):
            return store.pop_all(task_id)
        return await store.pop_all(task_id)
    except Exception:  # noqa: BLE001
        return []


async def peek_instructions(task_id) -> list[PendingInstruction]:
    """读取但不消费（用于 /live 展示待生效指令）。"""
    store = _get_store()
    try:
        if isinstance(store, _MemoryInstructionStore):
            return store.peek(task_id)
        return await store.peek(task_id)
    except Exception:  # noqa: BLE001
        return []
