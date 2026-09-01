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

from pobi_agent.logging import logger
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
    """同步入口，供同步 EventHooks.is_interrupted 调用。

    memory 后端直接查进程内集合；redis 后端用**同步** redis 客户端查 SET，
    绕开 ``run_coroutine_threadsafe`` + ``get_event_loop()`` 在跨线程调用时
    可能死锁 / 拿到错误 loop 导致超时返回 False 的问题（协作式取消不生效的根因之一）。
    任何异常一律按「未取消」处理，避免取消检查本身抛错中断 Agent。
    """
    try:
        store = _get_store()
        if isinstance(store, _MemoryCancelStore):
            return store.is_set(task_id)
        # redis 后端：使用 redis 同步客户端直查，避免事件循环桥接死锁
        import redis as sync_redis

        client = sync_redis.from_url(settings.redis_url, decode_responses=True)
        return bool(client.sismember("pobi_v2:cancelled", str(task_id)))
    except Exception:
        logger.exception("is_cancelled_sync 查询失败，按未取消处理")
        return False
