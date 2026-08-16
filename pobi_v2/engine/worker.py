"""ARQ Worker 入口。

启动：uv run arq pobi_v2.engine.worker.Worker

Worker 进程内直接调用 CoreAgent.run()，与原 pobi 的子进程 stdio 模型解耦，
可水平扩展，并复用事件总线实时推流。
"""
from __future__ import annotations

from arq import Worker, cron

from pobi_v2.engine.agent_adapter import install_event_hooks
from pobi_v2.engine.executor import run_task
from pobi_v2.engine.queue import REDIS_SETTINGS

# 任务执行超时（秒），供对账逻辑引用，避免魔法数字重复
JOB_TIMEOUT = 60 * 60 * 6  # 6h，渗透任务可能较长


async def _on_startup(_ctx: dict) -> None:
    """Worker 进程启动时安装事件钩子。

    Worker 是独立进程，不加载 FastAPI app，因此 main.py 中的
    ``install_event_hooks()`` 不会在此进程执行。必须在 on_startup 显式安装，
    否则 ``get_event_hooks()`` 返回 NullEventHooks，而它缺少 emit_phase_changed
    等方法，会导致 run_task 在驱动 DeadEndAgent 时立即抛 AttributeError。
    """
    install_event_hooks()


async def _auto_reconcile(_ctx: dict) -> None:
    """周期性任务对账：回收『幽灵任务』与取消残留。

    报告 C 暴露的核心问题——任务被 cancel 后 DB 仍 running、Worker 真实状态已脱节，
    最终只能手动调 ``POST /api/v1/system/task-reconcile`` 才终止。本 cron 每 5 分钟
    自动执行同样的对账逻辑，使取消请求在分钟级内自动生效，无需人工触发。

    ``task_reconcile`` 是 FastAPI 端点函数，但其函数体不依赖 request/user，可直接调用。
    """
    try:
        from pobi_v2.routers.system import task_reconcile

        await task_reconcile()
    except Exception:  # noqa: BLE001 — 对账失败不应影响 Worker 正常消费
        pass


class WorkerSettings:
    functions = [run_task]
    redis_settings = REDIS_SETTINGS
    # Worker 启动钩子：安装事件总线钩子，使多智能体事件能推流到前端
    on_startup = _on_startup
    # 任务执行超时（秒）
    job_timeout = JOB_TIMEOUT
    # 失败后重试次数
    max_tries = 2
    # 健康检查保留
    keep_result = 3600
    # 周期性任务对账：每 5 分钟回收幽灵任务 / 取消残留（报告 C）
    cron_jobs = [
        cron(_auto_reconcile, minute={0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55}),
    ]


def main() -> None:
    Worker(WorkerSettings).run()


if __name__ == "__main__":
    main()
