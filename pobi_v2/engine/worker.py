"""ARQ Worker 入口。

启动：uv run arq pobi_v2.engine.worker.Worker

Worker 进程内直接调用 CoreAgent.run()，与原 pobi 的子进程 stdio 模型解耦，
可水平扩展，并复用事件总线实时推流。
"""
from __future__ import annotations

from arq import Worker, cron

from pobi_v2.engine.executor import run_task
from pobi_v2.engine.queue import REDIS_SETTINGS


class WorkerSettings:
    functions = [run_task]
    redis_settings = REDIS_SETTINGS
    # 任务执行超时（秒）
    job_timeout = 60 * 60 * 6  # 6h，渗透任务可能较长
    # 失败后重试次数
    max_tries = 2
    # 健康检查保留
    keep_result = 3600


def main() -> None:
    Worker(WorkerSettings).run()


if __name__ == "__main__":
    main()
