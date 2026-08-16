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

Both trees share the same hierarchy:

    agents/<agent_id>/<target_slug>/

``agent_id`` is the resumable session UUID.
``target_slug`` is the filesystem-safe target identifier (e.g. ``localhost_8080``).
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
REUSABLE_CREDENTIALS_FILE: Path = ROOT_DEADEND_PATH / "reusable_credentials.json"
DEADEND_PROMPTS_PATH = ROOT_DEADEND_PATH / "prompts"

CACHE_DEADEND_AGENTS_PATH = CACHE_DEADEND_PATH / "agents"
CACHE_TRACES_PATH = CACHE_DEADEND_PATH / "traces"
CACHE_METRICS_PATH = CACHE_DEADEND_PATH / "metrics"
CACHE_TOOL_RESULTS = CACHE_DEADEND_PATH / "tool_results"
CACHE_DEADEND_LOGS = CACHE_DEADEND_PATH / "logs"



