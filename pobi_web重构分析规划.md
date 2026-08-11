# Pobi 项目分析与 Web 平台重构规划

> 本文档按"先建立上下文 → 再定义边界 → 最后分阶段落地"的三步方法论产出，
> **仅做分析与规划，不输出任何实现代码**。
> 分析基于当前仓库真实代码（README、CoreAgent、Web Console、Agents、Tools、Settings）。

---

## 一、项目分析（上下文注入）

### 1.1 项目定位

Pobi 是一个**本地优先的自主 Web 应用渗透测试 Agent**，采用反馈驱动迭代(feedback-driven iteration)适配利用策略。它基于 **deadend-cli** 演进而来——代码中存在大量 `DEADEND_*` 环境变量前缀和 `deadend-cli` 注释（如 `core_agent.py` 的 `DEADEND_LLM_MAX_CONCURRENCY`、api.py 中 "the original deadend-cli awaits embedTarget"），证实二者同源。

核心能力：
- **模型无关**：经 LiteLLM 接入任意可部署 LLM（实测 Kimi K2.5 在 XBOW 验证集约 80%）。
- **自研沙箱工具**：Playwright(浏览器)、Docker(Kali)、WASM(Python 拦截器)。
- **ADaPT 架构**：Supervisor + Subagent 层级编排。
- **置信度决策引擎**(护城河)：<20% 放弃 / 20–60% 扩展 / 60–80% 精调 / >80% 验证。
- **本地运行**：数据不外泄，配置存 `~/.pobi/config.json`。

### 1.2 核心架构梳理

#### (1) Agent 编排：分层 Supervisor–Subagent（ADaPT）

- `pobi_agent/agents/supervisor_agent.py`：维护高层目标，派发子任务。
- `pobi_agent/agents/generic_agents/`、`exploit_web_agent.py`、`recon_threatmodel_agent.py`、`planner.py`、`judge.py`、`validator.py`、`reporter.py`：各类专职 Subagent。
- `pobi_agent/agents/architecture.py`、`factory.py`：Agent 工厂与架构装配。
- **决策由置信度驱动**：`judge.py`（`validation_strategies.py`）给出置信度，决定继续哪条分支；`scope.py` 提供授权范围闸门。

#### (2) Agent 引擎：`CoreAgent`（通用异步 LLM 循环）

`pobi_agent/core_agent/core_agent.py` 是当前最关键的复用资产：

- 一个与具体业务无关的 **ReAct 式循环**：`run()` → `_run_impl()` 中 `while` 调 LLM → 执行 tools → 回填结果 → 直到 `finish_reason=="stop"`，最多 50 轮。
- 用 **LiteLLM** 做统一模型接入，**Instructor** 做结构化输出（带手动 JSON 回退）。
- **工具自动注册**：`tool()` 装饰器 + `_build_tool_schemas()` 从函数签名/类型注解自动生成 OpenAI tool schema，无需手写 JSON。
- 内置：tenacity 重试（限流/超时/连接错误）、`AsyncLimiter` 限流、`asyncio.Semaphore` 跨实例并发限制、OpenTelemetry/OpenInference 追踪、用量计数器。
- 通过 `hooks.emit_*` 把思考/工具/错误事件推到事件总线——这正是 Web 实时推送的源头。

> 这与 deadend-cli 的 OODA 决策循环思想一致，且已天然异步化，可直接作为 Web 平台的"决策内核"。

#### (3) 工具链集成

- `pobi_agent/tools/`：
  - `shell.py`、`web_resource_extractor.py`、`webapp_code_rag.py`、`tool_wrappers.py`：基础工具。
  - `browser/`、`browser_automation/`、`avfs/`(AI 虚拟文件系统)、`python_interpreter/`：沙箱型工具。
  - `webapp_analyzer/`：指纹分析。
- 工具以**普通 async 函数 + 类型注解**编写，`CoreAgent` 自动装配；带 `deps/ctx/context` 注入点。

#### (4) 状态管理（当前实现）

- **单用户、进程内 + JSON 文件**：
  - `web_console/settings.py`：`data_dir` 默认 `web_console_data` 目录。
  - `web_console/api.py`：`sessions.json`(`HISTORY_FILE`) 持久化会话快照，`audit_store` 存审计日志，`session_index` 内存字典做 session→agent 映射。
  - 配置：`~/.pobi/config.json`。
- **没有** PostgreSQL、没有任务队列、没有多用户鉴权。

### 1.3 技术栈与关键依赖

- 语言：Python 3.11+（uv workspace 管理）。
- LLM 层：`litellm`、`instructor`、`pydantic-ai`(RunUsage)。
- Web：`fastapi`、`sse-starlette`、`uvicorn`、`starlette`。
- RPC：自研 `jsonrpc_server.py` / `rpc_server.py`（子进程 stdio 通信）。
- 沙箱：`docker` SDK、`playwright`、`python_sandbox_client`(git submodule)、WASM。
- 可观测：`opentelemetry`、`openinference` semconv。
- 前端：`web_console/static/` —— **原生 HTML/CSS/JS**（非 React/Vue 框架）。
- RAG/向量：`rag/`、`embedders/`、`chromadb` 类依赖。

### 1.4 作为 CLI/本地工具的局限性（重构为 Web 平台的障碍）

| 维度 | 现状 | 局限 |
|------|------|------|
| 用户模型 | 单用户、本地进程 | 无法多人协作、无法隔离租户 |
| 存储 | JSON 文件 + 内存字典 | 并发写易损、无查询能力、无历史聚合 |
| 任务执行 | 子进程 stdio 阻塞式 `run_agent_recursive` | 无独立任务队列，进程崩即丢状态 |
| 鉴权 | 仅 `x-operator` header 占位 | 无 AuthN/AuthZ，任何人可调用 |
| 实时性 | SSE 已存在但绑定单 daemon | 多任务并发流难隔离、重启丢上下文 |
| 前端 | 原生 JS | 规模化功能难维护 |
| 成本管控 | 仅 token 计数 | 无 run/agent 级预算硬停 |

### 1.5 设计亮点与明显缺陷

**亮点**
1. `CoreAgent` 引擎与业务解耦，工具自动 schema 化，复用成本极低。
2. 置信度决策 + scope 闸门构成"安全护栏"，天然契合审计合规。
3. 事件钩子(`hooks.emit_*`)已是流式输出雏形，改造 SSE 推流成本低。
4. 自研沙箱(AVFS/Python 拦截器/Docker)规避 LLM 直接触网的安全风险。

**缺陷**
1. 状态持久化脆弱（JSON + 内存），无事务、无并发控制。
2. Web Console 与 daemon 通过子进程 stdio 耦合，水平扩展几乎不可能。
3. 前端无框架，缺乏组件化，复杂交互（资产图、报告编辑器）难以承载。
4. 鉴权形同虚设(`x-operator` 写死)，生产部署有合规风险。
5. 无任务队列，长时渗透任务无法重试/续跑/取消。

---

## 二、重构目标与边界

### 2.1 保留的核心逻辑（复用，不重写）

- **决策内核**：`CoreAgent` 的异步 ReAct 循环、LiteLLM/Instructor 接入、自动 tool schema、重试/限流/追踪。
- **Agent 层级**：ADaPT Supervisor–Subagent 关系与 `factory.py` 装配逻辑。
- **置信度引擎**：`judge.py` + `validation_strategies.py` 的决策阈值逻辑。
- **工具实现**：`tools/` 下全部沙箱工具（浏览器/AVFS/Python 拦截器/Shell/提取器）。
- **护栏**：`scope.py` 授权范围闸门、`hooks` 事件总线。
- **结构化输出**：漏洞/报告的 Pydantic 模型与 `reporter.py`。

### 2.2 需要重新设计的部分

- **CLI/子进程 → 前后端分离**：FastAPI 后端（已部分具备）+ 现代前端框架。
- **同步阻塞 → 异步任务队列 + SSE 实时推送**：引入 Celery/RQ/ARQ，任务与 HTTP 解耦。
- **本地文件 → PostgreSQL + 对象存储**：会话、任务、漏洞、审计均入库；产物(PoC/截图)存对象存储。
- **新增**：多用户认证(AuthN)、基于角色的鉴权(AuthZ)、任务管理（CRUD/取消/续跑）、审批护栏（高危操作人工确认）、审计日志（已有雏形，需结构化入库）、成本预算硬停。

### 2.3 新系统模块划分（文字图）

```
┌─────────────────────────────────────────────┐
│  Frontend (React/Vue)                         │
│  目标管理 / 任务看板 / 实时流 / 报告 / 审计    │
└───────────────┬─────────────────────────────┘
                │ HTTPS + Bearer Token
┌───────────────▼─────────────────────────────┐
│  API Gateway (FastAPI)                        │
│  Auth(登录/JWT/权限) · 限流 · 审批护栏中间件  │
└───┬───────────┬───────────────┬─────────────┘
    │           │               │
┌───▼───┐ ┌─────▼──────┐ ┌─────▼──────┐
│ Target│ │  Task API   │ │ Audit API  │  (CRUD + 业务接口)
└───────┘ └─────┬──────┘ └────────────┘
                │ 入队
┌───────────────▼─────────────────────────────┐
│  Task Queue (Celery/ARQ) + Worker Pool        │
│  每个 Task 起一个 CoreAgent 实例（沙箱隔离）   │
│  → 经 hooks 推送 SSE → 经护栏拦截高危工具调用  │
└───────────────┬─────────────────────────────┘
                │ 读写
┌───────────────▼───────────────┐  ┌──────────────┐
│  PostgreSQL (SQLAlchemy+Alembic)│  │ 对象存储 MinIO│
│  targets/tasks/findings/audit    │  │ PoC/截图/产物 │
└────────────────────────────────┘  └──────────────┘
```

### 2.4 可复用代码清单（精确到文件/函数级）

| 来源 | 复用点 | 新位置 |
|------|--------|--------|
| `core_agent/core_agent.py` | `CoreAgent.run/_run_impl`、`tool()` 装饰器、`_build_tool_schemas`、`hooks` | 后端 `engine/` 内核（原样复用） |
| `core_agent/core_agent.py` | `_emit_thinking/_emit_tool_call/_emit_tool_result/_emit_error` | Worker → SSE 推送适配 |
| `agents/factory.py`、`architecture.py` | `build_*_agent` 装配 | `engine/agent_factory.py` |
| `agents/judge.py`、`components/validation_strategies.py` | 置信度判定 | `engine/decision.py` |
| `agents/scope.py` | `is_in_scope` 闸门 | `engine/guardrails.py` |
| `tools/*` | 全部工具实现 | `engine/tools/`（加沙箱资源注入） |
| `reporter.py` + 报告 Pydantic 模型 | 结构化输出 | `engine/reporting.py` + `schemas/` |
| `web_console/api.py` | `_generate_session_id`、`run_agent_recursive` 编排思路、SSE 事件格式 | 重构为 `routers/tasks.py` |
| `web_console/audit_store.py` | 审计事件结构 | `models/audit.py` + 入库 |
| `web_console/validation_config.py`、`settings.py` | 配置项 | `core/config.py` |

### 2.5 需要重写的部分及原因

1. **`web_console/api.py` 的会话管理**：JSON 文件 + 内存字典 → 改为 DB 模型 + Task Queue，原因：并发/持久化/可扩展。
2. **`daemon_bridge.py` 子进程 stdio 模型**：改为 Worker 进程内直接 `CoreAgent.run()`，原因：去耦合、可水平扩展、便于续跑。
3. **前端 `static/`**：原生 JS → React/Vue，原因：复杂交互与状态管理需求。
4. **`settings.py` 配置**：`DEADEND_*` 环境变量 → 统一 Pydantic settings + `.env`，原因：可配置化部署。
5. **鉴权**：`x-operator` 占位 → JWT + RBAC，原因：生产合规。

### 2.6 技术选型及理由

- **后端**：FastAPI（已采用，异步原生，与 `CoreAgent` 协程模型契合）。
- **ORM/迁移**：SQLAlchemy 2.0 + Alembic（类型安全、生态成熟）。
- **数据库**：PostgreSQL（JSONB 存灵活 schema、事务可靠）。
- **任务队列**：**ARQ**（基于 Redis、纯 asyncio，与 FastAPI 同事件循环，比 Celery 更轻、无多进程模型冲突）/ 或 Celery（若需复杂调度）。
- **实时推送**：`sse-starlette`（已用，延续）。
- **对象存储**：MinIO（S3 兼容，本地可跑）。
- **前端**：React + Vite + TypeScript（或 Vue3）；状态用 TanStack Query + Zustand。
- **Auth**：JWT(access+refresh) + 密码哈希(bcrypt)；RBAC 表。
- **理由**：尽量复用既有栈（FastAPI/SSE/LiteLLM），降低重写风险；异步队列选 ARQ 避免阻塞事件循环。

### 2.7 分阶段 MVP 路线图（6 个里程碑）

- **M1 后端骨架 + 目标/任务管理**：FastAPI + SQLAlchemy + Alembic，Target/Task CRUD，复用 `CoreAgent` 作适配层，类型注解 + Pydantic + 基础异常。
- **M2 Agent 引擎接入任务队列**：ARQ Worker 承载 `CoreAgent`，SSE `/api/v1/tasks/{id}/stream` 实时推思考过程，护栏中间件拦截高危调用。
- **M3 持久化与产物**：PostgreSQL 落库（findings/audit），MinIO 存 PoC/截图，会话可续跑/取消。
- **M4 用户认证与鉴权**：注册/登录 JWT，RBAC，接口加 Auth 依赖，替换 `x-operator`。
- **M5 审批护栏 + 审计闭环**：高危工具调用人工确认流，审计日志结构化入库 + 查询接口。
- **M6 前端重构 + 报告中心**：React 重写控制台，目标/任务看板/实时流/报告预览/审计视图；成本预算硬停。

---

## 三、下一步（落地步骤预览，待你确认后再写代码）

> 以下仅为 M1 的实施清单草稿，未执行：
> 1. 引入依赖：`fastapi`、`sqlalchemy`、`alembic`、`pydantic-settings`、`psycopg[binary]`。
> 2. 新建 `web_platform/` 包：`core/config.py`、`db/session.py`、`models/{target,task,user,audit}.py`、`schemas/`、`routers/{targets,tasks,auth}.py`、`engine/`（搬运 `CoreAgent`）。
> 3. 编写 Alembic 初始迁移；提供 `docker-compose.yml`（PostgreSQL + Redis + MinIO）。
> 4. 实现 Target/Task CRUD 接口与基础异常处理中间件。

**确认后我将从里程碑 1 开始逐模块落地。** 你也可以先就上述分析/选型/路线图提出修正。
