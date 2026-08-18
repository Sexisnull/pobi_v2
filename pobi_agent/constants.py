# Copyright (C) 2025 Yassine Bargach
# Licensed under the GNU Affero General Public License v3
# See LICENSE file for full license information.
"""Canonical path constants for the DeadEnd agent storage layout.

Two roots are used:

* ``<POBI_HOME>/`` — persistent data that should survive across runs
  (config, credentials, per-agent DBs, auth contexts, crawled webpages).

* ``<POBI_CACHE_HOME>/`` — runtime data
  (traces, context dumps, metrics, tool JSONL results).

``POBI_HOME`` defaults to ``~/.pobi_v2`` (this project's isolated data dir)
and can be overridden via the ``POBI_HOME`` environment variable. Both the
persistent root and the cache root live under ``POBI_HOME`` so that *all*
runtime data stays inside the single host-mounted directory and survives
container restarts.

任务级产物统一归口到 ``TASKS_ROOT = <POBI_HOME>/tasks``，按
``tasks/<task_id>/`` 单级组织（``task_id`` 即内核 ``session_id``），
便于按任务 id 直接查询。内核仅持有 ``task_id``，不感知授权目标 slug，
因此统一以任务 id 为目录主键，平台层负责把 ``task_root`` 注入
``pobi_agent.storage_context`` 供内核各写入点复用。

单次任务目录结构约定::

    tasks/<task_id>/
        scope.<task_id>.yaml        # 平台层：授权范围
        validation.<task_id>.yaml   # 平台层：验证策略
        agent/                      # 内核 agent_storage_root
            <agent_id>/<session_id>/workspace
            <agent_id>/<session_id>/memory
            <agent_id>/<session_id>/run_context
            <agent_id>/<session_id>/auth_context
        rag/                        # RAG 索引（白盒分析）
        logs/
            python_interpreter.jsonl
            requester.jsonl
        metrics/
            metrics.json

历史旧路径（``agents/<agent_id>/<task_id>/``、``targets/<slug>/<task_id>/``、
``cache/logs/<task_id>/``、``cache/metrics/<task_id>/``）属早期契约，已弃用；
新增写入点统一经 ``storage_context.get_task_root()`` 落到 ``tasks/<task_id>/``，
请勿再使用 ``DEADEND_AGENTS_PATH`` / ``CACHE_DEADEND_LOGS`` / ``CACHE_METRICS_PATH``
等旧根。
"""
from __future__ import annotations
import os
from pathlib import Path

# 持久层根：默认 ~/.pobi_v2，允许通过环境变量覆盖（docker 内设为 /root/.pobi_v2，
# 与宿主 ~/.pobi_v2 挂载对齐，确保 ContextEngine 等产物落盘且跨重启保留）。
ROOT_DEADEND_PATH = Path(os.getenv("POBI_HOME", Path.home() / ".pobi_v2"))

# 缓存层根：默认落在持久层根下的 cache，同样可通过 POBI_CACHE_HOME 覆盖。
CACHE_DEADEND_PATH = Path(os.getenv("POBI_CACHE_HOME", ROOT_DEADEND_PATH / "cache"))

MODEL_CONFIG_PATH = ROOT_DEADEND_PATH / "config.json"
SETTINGS_CONFIG_PATH = ROOT_DEADEND_PATH / "settings.json"
DEADEND_AGENTS_PATH = ROOT_DEADEND_PATH / "agents"
DEADEND_VALIDATION_CONFIG_PATH = ROOT_DEADEND_PATH / "validation.yaml"
# 统一任务目录根：tasks/<task_id>/ 聚合单次运行所有产物
# (scope/validation/agent 工作区/RAG/logs/metrics)，按任务 id 归口，便于查询。
TASKS_ROOT = ROOT_DEADEND_PATH / "tasks"
REUSABLE_CREDENTIALS_FILE: Path = ROOT_DEADEND_PATH / "reusable_credentials.json"
DEADEND_PROMPTS_PATH = ROOT_DEADEND_PATH / "prompts"

CACHE_DEADEND_AGENTS_PATH = CACHE_DEADEND_PATH / "agents"
CACHE_TRACES_PATH = CACHE_DEADEND_PATH / "traces"
CACHE_METRICS_PATH = CACHE_DEADEND_PATH / "metrics"
CACHE_TOOL_RESULTS = CACHE_DEADEND_PATH / "tool_results"
CACHE_DEADEND_LOGS = CACHE_DEADEND_PATH / "logs"



