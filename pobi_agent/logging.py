# Copyright (C) 2025 Yassine Bargach
# Licensed under the GNU Affero General Public License v3
# See LICENSE file for full license information.

"""Centralized logging configuration for pobi.

This module provides a single logger instance and setup function
that can be used across all modules in pobi_agent and pobi.
Logs are sent to stderr to avoid interfering with stdout-based protocols
like JSON-RPC.

Usage:
    from pobi_agent.logging import logger, setup_logging

    # Setup logging at startup (typically in RPC server or main)
    setup_logging(level=logging.DEBUG)

    # Use logger anywhere
    logger.debug("Debug message")
    logger.info("Info message")
"""

from __future__ import annotations

import logging
import sys
from typing import Optional

# Package-wide logger name
LOGGER_NAME = "pobi"

# Create the package logger
logger = logging.getLogger(LOGGER_NAME)


class _TaskLoggerAdapter(logging.LoggerAdapter):
    """把 task_id 自动拼进每条日志 message 前缀，不改全局 formatter。"""

    def process(self, msg, kwargs):
        tid = self.extra.get("task_id") if self.extra else None
        if tid is not None:
            return f"(task_id={tid}) {msg}", kwargs
        return msg, kwargs


def task_logger(task_id) -> logging.LoggerAdapter:
    """返回带 task_id 上下文的 logger adapter。

    用法：
        log = task_logger(tid)
        log.info("任务主体加载 | kind=%s", kind)
    → 输出: ... [pobi:LINE] (task_id=xxx) 任务主体加载 | kind=probe

    通过拼前缀方式注入，保持全局 formatter 不变，旧日志格式兼容。
    """
    return _TaskLoggerAdapter(logger, {"task_id": task_id})

# 统一日志格式：日期 时间(含毫秒) 时区 级别 模块:行号 消息
# 时区由容器 TZ=Asia/Shanghai 决定，格式中 %(asctime)s 自动带中国时间。
DEFAULT_FORMAT = "%(asctime)s %(levelname)-8s [%(name)s:%(lineno)d] %(message)s"
DEFAULT_DATEFMT = "%Y-%m-%d %H:%M:%S"


def setup_logging(
    level: int = logging.INFO,
    log_file: Optional[str] = None,
    format_string: Optional[str] = None,
    datefmt: Optional[str] = None,
) -> logging.Logger:
    """Configure the package-wide logger (and clean up third-party colored logs).

    Args:
        level: Logging level (e.g., logging.DEBUG, logging.INFO). Defaults to INFO.
        log_file: Optional path to log file. If provided, logs are also written there.
        format_string: Custom format. Falls back to a readable timestamped format.
        datefmt: Custom date format.

    Returns:
        The configured logger instance.
    """
    fmt = format_string or DEFAULT_FORMAT
    dfmt = datefmt or DEFAULT_DATEFMT
    formatter = logging.Formatter(fmt, datefmt=dfmt)

    # 强制重置 root logger：清除 arq / uvicorn / litellm 等第三方注入的
    # 彩色 ANSI handler，避免日志里出现 \x1b[92m 等转义码导致不可读。
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    root.setLevel(level)
    root.addHandler(logging.NullHandler())

    logger.setLevel(level)
    logger.handlers.clear()

    console_handler = logging.StreamHandler(sys.stderr)
    console_handler.setLevel(level)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    if log_file:
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setLevel(level)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    # 接管 root 传播：第三方库（arq/uvicorn/litellm）日志统一经本 formatter 输出，
    # 确保全平台日志格式一致、无颜色码。
    logger.propagate = False
    root.propagate = False
    # 将 root 的消息导向我们的 formatter（第三方库走 root）
    root.addHandler(console_handler)
    if log_file:
        root.addHandler(file_handler)

    return logger


def get_module_logger(module_name: str) -> logging.Logger:
    """Get a child logger for a specific module.

    Args:
        module_name: Name of the module (typically __name__).

    Returns:
        A child logger that inherits settings from the package logger.

    Usage:
        module_logger = get_module_logger(__name__)
        module_logger.debug("Module-specific debug message")
    """
    return logging.getLogger(f"{LOGGER_NAME}.{module_name}")


__all__ = ["logger", "setup_logging", "get_module_logger", "LOGGER_NAME"]
