"""取消状态存储（M3）。

Worker 运行期间，API 可通过 cancel_task 设置取消标志；CoreAgent 的
EventHooks.is_interrupted 查询该标志实现协作式取消。

后端：
- memory：进程内 set（单 worker / 开发），同步实现。
- redis：Redis SET（多 worker，跨进程），异步实现。

对外暴露的 request_cancel / clear_cancel / is_cancelled 统一为 async；
memory 后端内部用同步集合，redis 后端用异步客户端。
"""
from __future__ import annotations

import asyncio

from pobi_v2.core.config import settings


class _MemoryCancelStore:
    def __init__(self) -> None:
        self._flags: set[str] = set()

    def set(self, task_id) -> None:
        self._flags.add(str(task_id))

    def clear(self, task_id) -> None:
        self._flags.discard(str(task_id))

    def is_set(self, task_id) -> bool:
        return str(task_id) in self._flags


class _RedisCancelStore:
    def __init__(self, redis_url: str) -> None:
        import redis.asyncio as aioredis

        self._redis = aioredis.from_url(redis_url, decode_responses=True)
        self._key = "pobi_v2:cancelled"

    async def set(self, task_id) -> None:
        await self._redis.sadd(self._key, str(task_id))

    async def clear(self, task_id) -> None:
        await self._redis.srem(self._key, str(task_id))

    async def is_set(self, task_id) -> bool:
        return bool(await self._redis.sismember(self._key, str(task_id)))


_store: object | None = None


def _get_store():
    global _store
    if _store is not None:
        return _store
    if settings.event_bus_backend == "redis":
        _store = _RedisCancelStore(settings.redis_url)
    else:
        _store = _MemoryCancelStore()
    return _store


async def request_cancel(task_id) -> None:
    store = _get_store()
    if isinstance(store, _MemoryCancelStore):
        store.set(task_id)
    else:
        await store.set(task_id)


async def clear_cancel(task_id) -> None:
    store = _get_store()
    if isinstance(store, _MemoryCancelStore):
        store.clear(task_id)
    else:
        await store.clear(task_id)


async def is_cancelled(task_id) -> bool:
    store = _get_store()
    if isinstance(store, _MemoryCancelStore):
        return store.is_set(task_id)
    return await store.is_set(task_id)


def is_cancelled_sync(task_id) -> bool:
    """同步入口，供同步 EventHooks.is_interrupted 调用（memory 后端）。"""
    store = _get_store()
    if isinstance(store, _MemoryCancelStore):
        return store.is_set(task_id)
    # redis 后端：回退到事件循环桥接
    future = asyncio.run_coroutine_threadsafe(
        store.is_set(task_id), asyncio.get_event_loop()
    )
    return future.result(timeout=2)
