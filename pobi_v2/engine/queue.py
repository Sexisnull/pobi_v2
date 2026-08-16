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

    若同一 task_id 已存在于队列（手动重复入队 / 前端重复点击），ARQ 会因 job_id
    唯一约束抛异常。此处捕获并幂等忽略——任务已在调度中，无需重复入队。

    ``kind``：链路连通性探针（``probe``）使用独立的短超时与 ``max_tries=1``，
    避免探测失败无谓重试 2 次、或占用 Worker 长达 6h（见 ``worker.py`` 默认配置）。
    """
    redis = await get_redis()

    # 说明：arq 0.28 的 enqueue_job 仅支持 _job_id/_queue_name 等保留前缀键，
    # job_timeout/max_tries 会被透传为任务函数参数导致签名报错，故不在此处设置。
    # 探针的"快速结束"由 executor._run_probe_branch 内的 asyncio.wait_for(90s)
    # 硬超时保证，绝不会挂死 Worker。
    try:
        await redis.enqueue_job("run_task", task_id, _job_id=task_id)
    except Exception as exc:  # noqa: BLE001 — job_id 重复属预期，幂等忽略
        if "job_id" in str(exc).lower() or "exists" in str(exc).lower():
            return
        raise
    finally:
        await redis.close()


# ARQ 需要的任务函数引用（worker 通过 functions= 注册）
from pobi_v2.engine.executor import run_task  # noqa: E402,F401
