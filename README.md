# Pobi v2

前后端分离的 AI 渗透测试 Web 平台，重构自 `pobi`（deadend-cli 演进分支）。

本目录是一个**独立项目**，通过 uv workspace 复用父仓库 `pobi/pobi_agent`
（`CoreAgent` 决策内核、`DeadEndAgent` 编排、`EventHooks` 事件总线、
工具链与置信度护栏），但拥有独立的前后端架构、独立依赖与独立运行方式。

## 当前进度：里程碑 1–8（M8 已完全复刻原 AI 自主渗透系统，直接驱动原 DeadEndAgent）

- FastAPI 应用骨架
- SQLAlchemy 2.0 + Alembic（PostgreSQL）
- `Target` / `Task` CRUD 接口
- Pydantic Schema 与统一异常处理中间件
- `CoreAgent` 适配层 + 事件总线（`EventHooks` → 内存/Redis pub-sub 通道）
- **ARQ 任务队列**：创建任务即入队，Worker 进程内异步运行 `CoreAgent`
- **SSE 实时推送**：`GET /api/v1/tasks/{id}/stream` 实时推送思考/工具/置信度/状态
- **授权范围护栏**：基于 `pobi_agent.scope.ScopePolicy`，从 Target 的 scope 注入，越权创建任务直接拒绝
- **M3 持久化与产物落库**：运行轨迹 / 发现 / 审计 / 产物入库，支持取消与续跑
- **M4 多租户鉴权**：User/Tenant 模型 + JWT 认证 + 资源租户隔离，所有接口默认需 Bearer 令牌
- **M5 审批护栏 + 结构化报告**：高危工具调用需人工审批（fail-closed），任务报告 Markdown/JSON 导出
- **M6 前端 SPA**：纯静态单页应用（vanilla JS + SSE），由 FastAPI 直接挂载 `web/` 目录，零额外依赖、零构建步骤，覆盖登录/注册、目标与任务管理、实时事件流、审批决策、报告导出
- **M7 复刻 agent 扫描逻辑（轻量版）**：以 `ScanWorkflow` 复刻原 `DeadEndAgent` 的三阶段工作流（threat_model → supervisor/exploitation 循环 + ValidationGate → report）；**复用原 pobi_agent 的核心组件**（`ScopePolicy` 授权闸门、`ValidationGate` 置信度判定、`ReporterAgent` 报告、`CoreAgent`/`EventHooks`），并以 `scan_tools` 重新实现 HTTP 侦察（httpx）+ 受限 shell，出口复用 `ScopePolicy` 做 host/path 级 scope 闸门
- **M8 完全复刻原 AI 自主渗透系统（主路径）**：直接驱动原 `pobi_agent.pobi_agent.DeadEndAgent`（**完整多智能体协作系统**），而非轻量重写。`engine/deadend_runner.py` 作为集成适配层，把 pobi_v2 的 `Target`/`Task` 注入原引擎，并完整复用其全部能力：
  - **多智能体协作**：`SupervisorAgent`（pydantic_ai）通过工具委派调用 6 个专业子 Agent（requester / python_interpreter / shell / webapp_analyzer / memory / authenticator），每个子 Agent 返回结构化 `AgentOutput`（confidence_score / detailed_summary / proofs / thoughts）
  - **Docker 沙箱执行验证**：`python_interpreter` / `shell` 在真实 Docker 沙箱中执行 payload，验证漏洞可利用性（沙箱为必需依赖；无 Docker 时回退到 M7 的 `ScanWorkflow`）
  - **多 LLM**：通过 `ModelSpec(provider, model_name, api_key, base_url)` 支持 `anthropic` / `openai` / `openrouter` / `gemini` / `requesty` / `local`，读 `settings.model` 的 `scheme/model` 字符串选择
  - **ADaPT 递归规划**：`PlannerAgent` + `ADaPTAgent` 分解任务为子目标树
  - **ValidationGate**：`FlagStrategy` + `JudgeAgentStrategy` 做目标达成验证
  - **ReporterAgent**：生成最终报告
  - **授权范围闸门**：复用原 `ScopePolicy`——运行前把 `Target` 的 scope 写入全局 `~/.cache/pobi/scope.yaml`，原 `pw_requester` 网络出口处 `check_scope` 自动生效（不重写 scope 逻辑）
  - **事件总线**：通过全局已注册的 `PobiV2EventHooks`，安全事件实时写入 pobi_v2 DB + SSE（session_id == task_id）
  - **审批护栏**：通过 `make_approval_callback` 接入原 `set_approval_callback`（fail-closed）
- `docker-compose.yml`（PostgreSQL + Redis）

### 里程碑 2 新增端点
- `POST /api/v1/tasks`：创建任务 → 护栏校验 → 状态 `queued` → 入队 ARQ
- `POST /api/v1/tasks/{id}/enqueue`：将 pending/failed/cancelled 任务重新入队
- `GET /api/v1/tasks/{id}/stream`：SSE 实时事件流（思考/工具调用/置信度/状态流转）

### 里程碑 3 新增端点
- `POST /api/v1/tasks/{id}/cancel`：请求取消运行/排队中的任务（协作式取消）
- `GET  /api/v1/tasks/{id}`：任务详情（含 findings / artifacts / 事件计数）
- `GET  /api/v1/tasks/{id}/events`：有序运行轨迹（task_events，可回放）
- `GET  /api/v1/tasks/{id}/findings`：该任务发现的漏洞/风险点
- `GET  /api/v1/tasks/{id}/artifacts`：该任务的产物（截图/PoC/报告/日志元数据）
- `GET  /api/v1/audit`：全局结构化审计日志（可按 task/target/action 过滤）

### 里程碑 4 新增端点（鉴权）
- `POST /api/v1/auth/register`：注册用户（归属指定租户 slug），返回 JWT
- `POST /api/v1/auth/login`：邮箱/密码登录，返回 JWT
- `GET  /api/v1/auth/me`：当前登录用户信息
- `POST /api/v1/auth/tenants`：创建租户
- 所有 `/targets` `/tasks` `/audit` 等接口默认需 `Authorization: Bearer <JWT>`，且仅返回当前租户资源

### 里程碑 5 新增端点（审批护栏 + 报告）
- `GET  /api/v1/approvals`：列出本租户审批请求（可按 status 过滤）
- `GET  /api/v1/approvals/{id}`：审批请求详情
- `POST /api/v1/approvals/{id}/decision`：批准/拒绝（fail-closed，超时/拒绝均拦截）
- `GET  /api/v1/tasks/{id}/report`：结构化报告（JSON）
- `GET  /api/v1/tasks/{id}/report/markdown`：Markdown 报告导出
- `GET  /api/v1/tasks/{id}/report/json`：JSON 报告导出

### 里程碑 6 新增（前端 SPA）
- 纯静态单页应用，由后端直接托管于 `/app`，无需 Node / 构建步骤。
- 覆盖登录/注册、目标与任务 CRUD、SSE 实时事件流、审批决策、报告导出。

> 实现说明（关联修复）：
> - `/auth/register` 在开放注册模式下，若 `tenant_slug` 对应租户不存在则**自动创建租户**，使自助注册自洽。生产环境关闭开放注册（`POBI_V2_ALLOW_OPEN_REGISTRATION=false`）后，需先用 `POST /auth/tenants` 预建租户。
> - `Target.in_scope` / `out_of_scope` 以 **JSONB** 列存储（列表原生读写），前端直接获得数组。

## 目录结构

```
pobi_v2/
├── pyproject.toml
├── README.md
├── docker-compose.yml
├── alembic.ini
├── alembic/
│   ├── env.py
│   └── versions/
├── web/                     # M6 前端 SPA（由 FastAPI 直接挂载，无需构建）
│   ├── index.html
│   └── static/
│       ├── css/styles.css
│       └── js/app.js
└── pobi_v2/
    ├── __init__.py
    ├── main.py              # FastAPI 入口（挂载 /static、/app、/web/*）
    ├── core/
    │   ├── config.py        # 配置（pydantic-settings）
    │   └── exceptions.py    # 统一异常与 HTTP 映射
    ├── db/
    │   ├── session.py       # engine / session / Base
    │   ├── models.py        # Tenant/User/Target/Task/ApprovalRequest/Finding/AuditEvent/TaskEvent/Artifact
    │   └── persistence.py   # 落库辅助（事件/发现/审计/产物）
    ├── schemas/
    │   ├── target.py
    │   ├── task.py
    │   ├── persistence.py   # 查询返回 Schema
    │   ├── auth.py          # 用户/租户/令牌 Schema
    │   └── approval.py      # 审批请求 Schema
    ├── core/
    │   ├── config.py
    │   ├── exceptions.py
    │   ├── security.py      # JWT + bcrypt
    │   └── deps.py          # get_current_user 鉴权依赖
    ├── routers/
    │   ├── auth.py          # 注册/登录/me/租户
    │   ├── targets.py       # 租户隔离
    │   ├── tasks.py         # 含 cancel，租户隔离
    │   ├── stream.py        # SSE，租户隔离
    │   ├── persistence.py   # findings/events/artifacts/audit，租户隔离
    │   ├── approval.py      # M5 审批请求列表/决策
    │   └── report.py        # M5 报告导出
    └── engine/
        ├── event_bus.py     # 事件总线（对接 pobi_agent EventHooks）
        ├── agent_adapter.py # CoreAgent/PobiAgent 适配层（含审批回调挂载）
        ├── guardrails.py    # 授权范围护栏（复用 scan_tools.build_scope_policy）
        ├── scan_workflow.py # M7 扫描工作流（复刻 DeadEndAgent 三阶段）
        ├── scan_tools.py    # M7 HTTP 侦察(httpx)+受限 shell（复用 ScopePolicy 闸门）
        ├── executor.py      # 任务执行管线（驱动 scan_workflow / 落库/取消/续跑）
        ├── queue.py         # ARQ 队列
        ├── worker.py        # ARQ WorkerSettings
        ├── cancel_state.py  # 取消标志存储（memory/redis）
        ├── approval.py      # M5 审批引擎（判定/决策/回调）
        └── report.py        # M5 结构化报告渲染
```

## 快速开始

```bash
# 在仓库根目录（uv workspace 已配置）
uv sync

# 启动依赖
docker compose up -d

# 初始化数据库
uv run alembic upgrade head

# 启动后端
uv run uvicorn pobi_v2.main:app --reload --port 8000

# 启动任务队列 Worker（另开一个终端）
uv run arq pobi_v2.engine.worker.WorkerSettings
```

API 文档： http://localhost:8000/docs

### 前端访问

M6 引入纯静态前端 SPA，由后端直接托管，**无需 Node / 构建步骤**：

```bash
# 后端启动后直接访问
open http://localhost:8000/app
```

- 登录页：邮箱/密码登录，或开放注册（默认开启，生产请置
  `POBI_V2_ALLOW_OPEN_REGISTRATION=false`）。
- 主界面：左侧切换「任务 / 授权目标 / 审批 / 审计」。
  - **任务**：新建（校验授权范围 → 入队）、查看详情、SSE 实时事件流、取消/重入队、报告导出。
  - **授权目标**：管理 `in_scope` / `out_of_scope`，护栏据此拒绝越权任务。
  - **审批**：高危工具调用待审批时，可一键批准/拒绝（fail-closed）。
  - **审计**：按时间倒序查看全局审计事件。
- 静态资源挂载于 `/static`，SPA 路由走 `/web/*`（非资源回退 `index.html`）。

> 仅做本地文件打开（`file://`）不可用，必须经过后端 `/app` 以携带同源 Cookie 与 CORS。

### 端到端流程
1. `POST /api/v1/targets` 创建授权目标（填写 `in_scope` / `out_of_scope`）。
2. `POST /api/v1/tasks` 创建任务（自动校验授权范围 → 入队）。
3. 前端订阅 `GET /api/v1/tasks/{id}/stream` 实时查看 Agent 思考与工具调用。
4. Worker 执行完成后，任务状态流转为 `completed` / `failed`，结果写入 `Task.result`。

> 注意：M2 的 Worker 默认以 `CoreAgent` 运行（事件钩子实时推流已验证）。
> 完整渗透编排（威胁建模 / 利用 / 报告）可切换为 `DeadEndAgent`（见 `engine/executor.py`）。
> 沙箱工具（Docker/Playwright/AVFS）需要相应环境，M3 将产物落库。

## 生产部署

镜像构建采用 **多阶段 Dockerfile**（`Dockerfile.prod`），编译工具与运行时彻底隔离。

### 1. 配置环境变量

创建 `.env`（不提交仓库）：

```dotenv
# ---- 镜像仓库（AGENTS.md 规范：强制阿里云 ACR 前缀，禁止官方裸镜像）----
ACR_REGISTRY=registry.cn-hangzhou.aliyuncs.com/your-ns

# ---- 数据库 ----
POBI_V2_DB_USER=pobi
POBI_V2_DB_PASSWORD=<强密码>
POBI_V2_DB_NAME=pobi_v2

# ---- 安全 ----
POBI_V2_JWT_SECRET=<32 字节以上随机串，务必替换 dev 默认值>
POBI_V2_ALLOW_OPEN_REGISTRATION=false   # 生产关闭开放注册

# ---- LLM（透传给 pobi_agent.CoreAgent）----
POBI_V2_MODEL=openai/gpt-4o
OPENAI_API_KEY=<你的 key>
# OPENAI_BASE_URL=...  # 如需代理

# ---- 任务执行 ----
POBI_V2_ALLOW_SHELL_EXEC=false          # 受限 shell 高危，默认关闭
POBI_V2_TASK_MAX_TURNS=50
```

### 2. 构建并启动

```bash
# 构建镜像（强制 ACR 前缀）
docker build -f Dockerfile.prod -t ${ACR_REGISTRY}/pobi_v2:1.0.0 .

# 一键编排：postgres → redis → api → worker → web(nginx)
docker compose up -d --build

# 数据库迁移（首启或升级时）
docker compose exec api alembic upgrade head
```

启动顺序依赖：`postgres`/`redis` 健康检查通过 → `api`/`worker` 启动 → `web`(nginx) 反代就绪。
访问入口为 `http://<host>/`（nginx 已开启 Gzip 并正确透传 SSE 长连接）。

### 3. 服务说明

| 服务 | 镜像 | 职责 |
|------|------|------|
| `postgres` | `${ACR_REGISTRY}/postgres:16-alpine` | 主数据存储（SQLModel/Alembic） |
| `redis` | `${ACR_REGISTRY}/redis:7-alpine` | ARQ 队列 + 事件总线 + 取消状态 |
| `api` | `pobi_v2:1.0.0` | FastAPI（gunicorn+uvicorn），提供 `/api/v1` 与 SSE |
| `worker` | `pobi_v2:1.0.0` | `arq pobi_v2.engine.worker.WorkerSettings`，异步执行扫描 |
| `web` | `${ACR_REGISTRY}/nginx:1.27-alpine` | 静态 SPA 托管 + 反代 + Gzip |

### 4. Gzip 与 SSE

`nginx.conf` 已强制开启 Gzip（防止明文传输大文件），并对 `/api/v1/tasks/` 关闭代理缓冲，
保证 SSE 事件逐条实时推送（见 AGENTS.md「更新 Nginx 配置必须开启 Gzip」）。

### 5. 白盒代码分析（可选）

`pobi_agent.code_indexer.SourceCodeIndexer` 依赖 Playwright / Embedder 后端 / RAG，属重量级
可选能力。当前默认关闭，由 `engine/scan_workflow.resolve_whitebox_stage(enabled=False)`
控制；依赖齐备后启用，缺失时优雅降级为纯黑盒扫描，不阻断主流程。

### 6. 许可证与源码提供义务（AGPL-3.0）

本项目以 **AGPL-3.0** 发布（详见 `LICENSE` 与 `NOTICE`）。依据 AGPL 第 13 条，若以网络服务
形式向公众提供，须向用户提供对应**完整源代码**获取途径。建议在页面页脚提供仓库链接，
或按 `NOTICE` 说明的途径提供源码归档。内嵌的 `pobi_agent/` 衍生自上游 pobi，其版权与
许可证见 `pobi_agent/THIRD_PARTY_NOTICE.md`。

### 7. 发布流程（AGENTS.md 规范）

1. 本地源码修改验证通过后，更新 `version.txt` 与 `pyproject.toml` 版本号。
2. 检查 `Dockerfile.prod` 符合多阶段构建规范。
3. 提交：`git commit`（采用工程化客观表述，禁止主观营销词汇）。
4. Push 前确认是否打 Tag（`v*`）触发自动构建。
5. 部署前确认最新镜像已就绪，再执行 `start-prod.sh`（或 `docker compose up -d`）。

