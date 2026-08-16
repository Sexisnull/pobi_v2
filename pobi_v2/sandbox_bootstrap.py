"""启动引导：确保全局共享 Kali 沙箱容器就绪（全面容器化核心依赖）。

全面容器化后，Kali 作为 docker-compose 的常驻 service 由宿主机 daemon 创建，
所有 shell 命令与 Python 验证共用同一容器。本模块在应用启动（lifespan）阶段
确保该容器存在且处于 Running；缺失时按统一网络创建，失败则 fail-fast。
"""
from __future__ import annotations

import asyncio

from pobi_agent.logging import logger
from pobi_agent.sandbox.sandbox_manager import SandboxManager
from pobi_v2.core.config import settings

# 进程级 SandboxManager 单例复用
_manager: SandboxManager | None = None


def _get_manager() -> SandboxManager:
    global _manager
    if _manager is None:
        _manager = SandboxManager()
    return _manager


async def ensure_shared_kali_ready() -> None:
    """确保全局共享 Kali 沙箱容器就绪。

    在事件循环外以线程执行（Docker SDK 为同步阻塞），避免阻塞异步事件循环。

    Raises:
        RuntimeError: 若 Kali 容器无法连接 / 创建或健康检查失败。
    """
    try:
        manager = _get_manager()
        sandbox = await asyncio.to_thread(manager.get_or_create_shared_kali)
    except Exception as exc:
        raise RuntimeError(
            f"全局共享 Kali 沙箱不可用（{settings.kali_container_name}）：{exc}"
        ) from exc

    # 健康检查：容器可响应基础命令
    try:
        health = await asyncio.to_thread(
            sandbox.execute_command, "echo kali_ready", stream=False
        )
        if health.get("exit_code", 1) != 0:
            raise RuntimeError(
                f"Kali 沙箱健康检查失败：{health.get('stderr')}"
            )
    except Exception as exc:
        raise RuntimeError(f"Kali 沙箱健康检查失败：{exc}") from exc

    logger.info(
        "全局共享 Kali 沙箱就绪：%s (network=%s)",
        settings.kali_container_name,
        settings.sandbox_network,
    )
