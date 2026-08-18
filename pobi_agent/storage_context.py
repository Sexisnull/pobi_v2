"""
当前任务的产物根目录上下文（task_root = tasks/<task_id>）。

设计动机
--------
平台层在派发任务时把 ``task_root`` 写入本模块，内核各工具（python_interpreter、
browser_automation、session_metrics 等）通过 ``get_task_root()`` 取得当前任务的
产物根。内核不感知授权目标 slug（target slug），仅以 task_id 归口。

并发语义
--------
使用 ``contextvars.ContextVar`` 做协程级隔离。``worker_max_jobs > 1`` 时，
两个任务并发执行它们的协程不会互相覆盖对方的 ``task_root``——这是比
``threading.local``（线程级）和模块全局变量（进程级）更细的隔离粒度，
也是 asyncio 任务的标准隔离方式。

使用契约
--------
- 平台层在派发协程入口调用 ``set_task_root(task_root)``；
- 内核工具通过 ``get_task_root()`` 取得当前 task 的产物根；返回 ``None``
  表示未注入（CLI/单测场景），调用方需自行决定回退或报错；
- 任务结束（成功或异常）必须 ``clear_task_root()``，释放本协程上下文。
"""
from __future__ import annotations

import contextvars
from pathlib import Path

# 协程级隔离：同一进程内两个 asyncio 任务各自持有独立的 task_root。
_task_root_var: contextvars.ContextVar[Path | None] = contextvars.ContextVar(
    "pobi_task_root", default=None
)


def set_task_root(task_root: Path) -> object:
    """注入当前任务的产物根，返回 ``ContextVar.Token`` 供协程退出时复位。"""
    return _task_root_var.set(Path(task_root))


def get_task_root() -> Path | None:
    """取得当前任务的产物根（``tasks/<task_id>``），未注入则返回 ``None``。"""
    return _task_root_var.get()


def clear_task_root(token: object | None = None) -> None:
    """清除当前任务的产物根。``token`` 由 ``set_task_root`` 返回，可撤销注入。"""
    if token is not None:
        _task_root_var.reset(token)  # type: ignore[arg-type]
    else:
        _task_root_var.set(None)
