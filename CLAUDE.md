# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目定位

Pobi v2 是一个前后端分离的 AI 渗透测试 Web 平台，重构自 `pobi`/`deadend-cli`。它是一个**薄控制面**：FastAPI 负责 REST/SSE/鉴权/任务编排，真正的渗透测试能力由内嵌的 `pobi_agent` 引擎（`DeadEndAgent` 多智能体协作系统）提供。pobi_v2 **不重写** Agent 逻辑，而是通过适配层（`engine/deadend_runner.py`）把 Web 层的 Target/Task 注入原引擎并复用其全部能力。

## 关键架构（跨多文件才能理解的大图）

### 1. 双进程 + 共享存储
- `api`（FastAPI）：REST 接口、SSE 推送、鉴权；`main.py` 入口。
- `worker`（ARQ）：进程内异步执行渗透任务；`engine/worker.py` → `engine/executor.run_task`。
- 两者通过 **Postgres**（状态/产物）+ **Redis**（ARQ 队列 + 事件总线 pub/sub + 取消标志）解耦。开发用 `memory` 后端（单进程），生产用 `redis`（跨进程），由 `POBI_V2_EVENT_BUS_BACKEND` 切换。

### 2. 事件流（session_id == task_id 是核心连接键）
原 `pobi_agent` 通过全局 `EventHooks` Protocol 推事件。pobi_v2 在应用启动时 `install_event_hooks()` 安装 `PobiV2EventHooks`（`engine/event_bus.py`），把事件按 `session_id` 发布到 EventBus。`routers/stream.py` 的 SSE 端点按 `task_id` 订阅同一通道。**task_id 即 session_id**——改一方必须同步另一方。事件同时由 `db/persistence.record_task_event` 落库形成可回放轨迹。

### 3. 两条执行路径 + 沙箱回退
- **主路径（M8）**：`executor.run_task` → `deadend_runner.run_deadend_agent` 直接驱动原 `DeadEndAgent`（多智能体：Supervisor 委派 requester/python_interpreter/shell/webapp_analyzer/memory/authenticator + Docker 沙箱执行验证 + ADaPT 规划 + ValidationGate + ReporterAgent）。**Docker 沙箱是必需依赖**。
- **回退路径（M7）**：沙箱不可用时（`RuntimeError` 含「沙箱/Docker」），`executor` 捕获并降级到 `ScanWorkflow`（轻量 HTTP 侦察 + 受限 shell，不含沙箱验证），保证可运行。两条路径返回相同结构的产出字典，落库逻辑一致。
- 改执行编排时务必同时考虑两条路径与 `executor._persist_outcome` 的契约。

### 4. 授权范围（scope）双闸门——不要重写 scope 逻辑
- **入口护栏**：`engine/guardrails.py` 基于 DB `Target.in_scope/out_of_scope` 构造 `ScopePolicy`（复用 `scan_tools.build_scope_policy`），任务入队/执行前 `assert_in_scope` 校验。
- **运行期出口闸门**：原 `pobi_agent` 的 `pw_requester` 网络出口处 `check_scope` 读取全局 `~/.cache/pobi/scope.yaml`。`deadend_runner._write_scope_file` 在运行前把 Target 范围写入该文件，使原 scope 逻辑自动生效。
- **两处必须一致**：改 Target 的 scope 形态时，DB 侧（`models.Target` JSONB 列）与 `scope.yaml` 侧都要适配。

### 5. 多租户隔离 + 审批（fail-closed）
- 所有资源表带 `tenant_id`；`core/deps.get_current_user` 解析 Bearer JWT，路由层用 `task.tenant_id != user.tenant_id` 做隔离。
- 高危工具审批：`engine/approval.py` 的 `make_approval_callback` 接入原 `PobiAgent.set_approval_callback`；运行期命中高危工具 → 建 `ApprovalRequest`（pending）→ 回调轮询 DB 决策 → **超时默认 reject**（fail-closed）。
- 取消为协作式：API 设 `cancel_state` 标志 → `PobiV2EventHooks.is_interrupted` 查询 → Agent 自行停止。

### 6. 内嵌子包（vendored，非外部依赖）
`pobi_agent`、`pobi_prompts`、`python_sandbox_client` 的源码已**复制**到本仓库作为子包（见 `pyproject.toml` 的 `[tool.hatch.build.targets.wheel] packages`），不从 PyPI 安装。改这些包的代码即改本仓库代码；但其三方运行依赖必须在 `pyproject.toml` 的 `dependencies` 中声明。

## 常用命令

```bash
# 依赖（uv workspace）
uv sync

# 基础设施（Postgres + Redis）
docker compose up -d

# 数据库迁移
uv run alembic upgrade head

# 启动 API（开发）
uv run uvicorn pobi_v2.main:app --reload --port 8000

# 启动 ARQ Worker（另开终端，任务实际执行处）
uv run arq pobi_v2.engine.worker.WorkerSettings

# 测试（asyncio_mode=auto，无需标记）
uv run pytest
uv run pytest tests/test_guardrails.py          # 单文件
uv run pytest tests/test_guardrails.py::test_high_risk_tool_detection  # 单测

# 前端：纯静态 SPA，由后端托管于 /app，无需构建
open http://localhost:8000/app
```

API 文档：`http://localhost:8000/docs`

## 配置

所有配置走 `core/config.py` 的 `Settings`（pydantic-settings），环境变量前缀 `POBI_V2_`，`.env` 文件加载。关键项：
- `POBI_V2_MODEL`：LLM，格式 `provider/model`（如 `openai/gpt-4o`、`anthropic/claude-sonnet-4`），由 `deadend_runner._build_model_spec` 解析，provider 段映射到对应 API Key 环境变量。
- `POBI_V2_EVENT_BUS_BACKEND`：`memory`（开发）| `redis`（生产）。
- `POBI_V2_ALLOW_OPEN_REGISTRATION`：生产必须置 `false`。
- `POBI_V2_ALLOW_SHELL_EXEC`：受限 shell，默认关闭。
- LLM API Key 按 provider 取对应环境变量（`OPENAI_API_KEY` / `ANTHROPIC_API_KEY` / `OPEN_ROUTER_API_KEY` / `GEMINI_API_KEY` 等），**不**带 `POBI_V2_` 前缀。

## 工程规范（来自 AGENTS.md，强制）

- 生成内容与 Artifacts 使用**中文**。
- 改 `Dockerfile.prod` 必须保持**多阶段构建**，隔离编译工具与运行时。
- `docker-compose.yml` 镜像强制使用**阿里云 ACR 前缀**（`registry.cn-hangzhou.aliyuncs.com/...`），禁止官方裸镜像。
- 改 `nginx.conf` 必须保留 **Gzip**，并对 `/api/v1/tasks/` 关闭代理缓冲以保 SSE 实时性。
- 破坏性操作（`git reset --hard` / `git clean -fd` / 销毁容器）前必须获用户批准。
- Git commit 用客观工程化表述（如「规避/收敛」），禁止主观营销词（如「完美解决」）。
- 版本号同步维护在 `version.txt` 与 `pyproject.toml`。
- 代码审查禁止只看局部，须完整追溯调用链。

## 测试约定

- `pytest-asyncio` 为 `auto` 模式，异步测试函数无需 `@pytest.mark.asyncio`。
- 大部分测试（`test_smoke`/`test_guardrails`/`test_engine_tools`/`test_m5`/`test_scan_workflow`）不依赖外部 DB，可直接跑；端到端测试需 Postgres + `alembic upgrade head`。
- 测试构造 ORM 对象时直接设属性（如 `t.in_scope = [...]`），见 `tests/test_guardrails.py`。
