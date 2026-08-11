"""ARQ 任务队列配置与入队辅助。"""
from __future__ import annotations

from arq import ArqRedis, create_pool
from arq.connections import RedisSettings

from pobi_v2.core.config import settings

REDIS_SETTINGS = RedisSettings.from_dsn(settings.redis_url)


async def get_redis() -> ArqRedis:
    return await create_pool(REDIS_SETTINGS)


async def enqueue_task(task_id: str) -> None:
    """将任务加入队列（worker 异步执行 run_task）。"""
    redis = await get_redis()
    try:
        await redis.enqueue_job("run_task", task_id)
    finally:
        await redis.close()


# ARQ 需要的任务函数引用（worker 通过 functions= 注册）
from pobi_v2.engine.executor import run_task  # noqa: E402,F401
