"""pytest 全局配置。

在收集任何被测模块之前，把应用日志目录重定向到本机临时目录：
``pobi_v2/main.py`` / ``pobi_v2/engine/worker.py`` 的日志目录经
``POBI_V2_LOG_DIR``（默认 ``/app/logs``，容器内部署约定）解析。宿主机无
``/app``，若不在测试环境覆盖则 import 应用即抛 OSError，导致 test_smoke /
test_recon_api 等用例无法收集。此处统一注入，保持容器默认值不变。
"""
from __future__ import annotations

import os
import tempfile

os.environ.setdefault(
    "POBI_V2_LOG_DIR",
    os.path.join(tempfile.gettempdir(), "pobi_v2_test_logs"),
)
