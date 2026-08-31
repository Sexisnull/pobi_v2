"""router 访问本地 RECON 库的统一入口（分层收敛）。

架构约束「router 禁止直接调内核（pobi_agent），经 engine/ 适配」：本地 RECON
存储（``ReconStore``）与任务产物根（``TASKS_ROOT``）属内核 ``pobi_agent``，
router 原本直接 import 它们（``routers/tasks.py``）构成边界穿透。本模块把该
访问收拢到 engine 层，保持依赖方向 router → engine → pobi_agent。
"""
from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Any

from pobi_agent.constants import TASKS_ROOT
from pobi_agent.recon.store import ReconStore

logger = logging.getLogger(__name__)


def task_root(task_id: Any) -> Path:
    """任务产物统一根目录（内核常量 ``TASKS_ROOT/<task_id>``）。"""
    return TASKS_ROOT / str(task_id)


def task_recon_store(task: Any) -> ReconStore:
    """为指定任务构造本地 RECON 库 Store（路径约束在 ``task_root`` 内）。

    本地库可能尚未物化（任务未启动/未产出侦察数据），由 Store 只读方法
    内部旁路容错返回空结构，调用方无需预判文件存在性。
    """
    root = task_root(task.id)
    return ReconStore.for_task(str(task.id), task_root=str(root))


def delete_task_local_data(task_id: Any) -> None:
    """删除任务的本地缓存目录（供删除任务时清理孤儿目录）。

    删除失败不影响 DB 记录已删除的结果，仅记日志。
    """
    try:
        cache_dir = task_root(task_id)
        if cache_dir.exists():
            shutil.rmtree(cache_dir)
    except Exception:  # noqa: BLE001
        logger.exception("删除任务 %s 时清理本地缓存目录失败", task_id)
