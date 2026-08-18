"""ARQ 任务队列配置与入队辅助。"""
from __future__ import annotations

from arq import ArqRedis, create_pool
from arq.connections import RedisSettings

from pobi_v2.core.config import settings

REDIS_SETTINGS = RedisSettings.from_dsn(settings.redis_url)


async def get_redis() -> ArqRedis:
    return await create_pool(REDIS_SETTINGS)


async def enqueue_task(task_id: str, kind: str | None = None) -> None:
    """将任务加入队列（worker 异步执行 run_task）。

    关键：显式传入 ``_job_id=task_id``，使 ARQ 的 job_id 与 PG 的 task.id 一致。
    否则 ARQ 会生成随机 job_id，导致 ``task_reconcile`` 用 task_id 反查
    ``arq:queue`` / ``arq:in_progress`` 永远 miss，误把正在运行的任务判为 failed
    （幽灵任务）。详见 router 层 task_reconcile 的匹配逻辑。

    幂等性由 ARQ 原子保证：``enqueue_job`` 内部用 ``watch`` + 事务检查 job_key
    是否已存在，若重复入队（手动重复 / 前端重复点击）会**静默返回 None**，不会抛异常。
    故此处直接以返回值是否为 None 判定幂等命中，无需依赖异常文本匹配（旧实现靠
    ``"job_id" in str(exc)`` 字符串匹配，既脆弱又永不会触发）。

    ``kind``：链路连通性探针（``probe``）的"快速结束"由
    ``executor._run_probe_branch`` 内的 ``asyncio.wait_for(90s)`` 硬超时保证，
    绝不会挂死 Worker；重试开关统一在 worker.py 的 ``retry_jobs=False`` 控制。
    """
    redis = await get_redis()
    try:
        # enqueue_job 在 job_id 已存在时返回 None（ARQ 原子幂等），不抛异常。
        job = await redis.enqueue_job("run_task", task_id, _job_id=task_id)
        if job is None:
            # 幂等命中：同 task_id 已在队列中，无需重复入队。
            return
    finally:
        await redis.close()


# ARQ 需要的任务函数引用（worker 通过 functions= 注册）
from pobi_v2.engine.executor import run_task  # noqa: E402,F401
