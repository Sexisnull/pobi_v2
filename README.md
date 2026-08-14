# Pobi v2

前后端分离的 AI 渗透测试 Web 平台，重构自 `pobi`。

本目录是一个**独立项目**，通过 uv workspace 复用父仓库 `pobi/pobi_agent`
（`CoreAgent` 决策内核、`DeadEndAgent` 编排、`EventHooks` 事件总线、
工具链与置信度护栏），但拥有独立的前后端架构、独立依赖与独立运行方式。

## 核心能力

- **FastAPI 应用骨架**：SQLAlchemy 2.0 + Alembic（PostgreSQL），Pydantic Schema 与统一异常处理。
- **ARQ 任务队列**：创建任务即入队，Worker 进程内异步驱动渗透引擎。
- **SSE 实时推送**：`GET /api/v1/tasks/{task_id}/stream` 实时推送思考 / 工具调用 / 置信度 / 状态流转。
- **授权范围护栏**：基于 `pobi_agent.scope.ScopePolicy`，从 Target 的 scope 注入，越权创建任务直接拒绝。
- **多智能体渗透引擎（M8 主路径）**：直接驱动原 `pobi_agent.DeadEndAgent` 完整多智能体系统。Phase 1 侦查与 Phase 2 利用共用同一套 supervisor + 6 子 Agent 引擎，仅 goal prompt 不同；Docker 沙箱执行验证、ADaPT 递归规划、ValidationGate 置信度判定、ReporterAgent 报告全链复用原内核。无 Docker 时回退轻量 `ScanWorkflow`（M7）。
- **多 LLM 支持**：通过 `ModelSpec(provider, model_name, api_key, base_url)` 支持 `anthropic` / `openai` / `openrouter` / `gemini` / `requesty` / `local`。
- **多租户鉴权**：User/Tenant 模型 + JWT 认证 + 资源租户隔离，所有接口默认需 Bearer 令牌。
- **审批护栏**：高危工具调用需人工审批（fail-closed，超时 / 拒绝均拦截）。
- **持久化与产物落库**：运行轨迹 / 发现 / 审计 / 产物入库，支持取消与续跑。
- **Token 用量统计**：会话级 token 累计落库，按每百万 token 单价估算成本。
- **前端 SPA**：纯静态单页应用（vanilla JS + SSE），FastAPI 直接挂载 `web/`，零构建步骤。

## 里程碑进度

| 里程碑 | 内容 | 状态 |
|--------|------|------|
| M1 | FastAPI 骨架 / SQLAlchemy / CRUD / 事件总线 | ✅ |
| M2 | ARQ 任务队列 / SSE 实时推送 / 授权护栏 | ✅ |
| M3 | 持久化与产物落库 / 取消与续跑 | ✅ |
| M4 | 多租户鉴权（JWT + 资源隔离） | ✅ |
| M5 | 审批护栏 + 结构化报告导出 | ✅ |
| M6 | 前端 SPA（vanilla JS + SSE） | ✅ |
| M7 | 轻量扫描工作流（`ScanWorkflow`，Docker 缺失时回退） | ✅ |
| M8 | 完整复刻原 `DeadEndAgent` 多智能体系统（主路径） | ✅ |
| M8+ | 运行指令通道 / 系统状态对账 / 统一 LLM 抽象层预留 / Token 用量统计 | ✅ |

## API 接口

> 除 `/auth` 部分端点外，所有接口默认需 `Authorization: Bearer <JWT>`，并按租户隔离。
> `{task_id}` 为任务 ID，`{approval_id}` 为审批请求 ID。

### 鉴权（`/api/v1/auth`）

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/auth/register` | 注册用户（归属租户 slug），返回 JWT；开放注册模式下租户不存在则自动创建 |
| POST | `/auth/login` | 邮箱 / 密码登录，返回 JWT |
| GET | `/auth/me` | 当前登录用户信息 |
| POST | `/auth/tenants` | 创建租户 |

### 目标（`/api/v1/targets`）

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/targets` | 列出租户下的授权目标 |
| POST | `/targets` | 创建目标（`in_scope` / `out_of_scope` 以 JSONB 存储） |
| GET/PUT/DELETE | `/targets/{target_id}` | 目标 CRUD |

### 任务（`/api/v1/tasks`）

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/tasks` | 任务列表（含 token 三列） |
| POST | `/tasks` | 创建任务 -> 护栏校验 -> 状态 `queued` -> 入队 ARQ |
| GET | `/tasks/{task_id}` | 任务详情（含 findings / artifacts / 事件计数） |
| POST | `/tasks/{task_id}/enqueue` | 重新入队 pending / failed / cancelled 任务 |
| POST | `/tasks/{task_id}/cancel` | 协作式取消运行 / 排队中的任务 |
| GET | `/tasks/{task_id}/stream` | SSE 实时事件流（思考 / 工具调用 / 置信度 / 状态） |
| GET | `/tasks/{task_id}/live` | 实时态聚合（当前阶段 / 智能体 / 计划 / 待生效指令） |
| POST | `/tasks/{task_id}/instructions` | 向运行中任务追加指令，协作式检查点消费注入上下文 |
| GET | `/tasks/usage/summary` | 全部任务 token 用量汇总（含已完成任务拆分） |
| GET | `/tasks/{task_id}/usage` | 单任务 token 用量明细 |

### 持久化查询（`/api/v1`）

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/tasks/{task_id}/events` | 有序运行轨迹（可回放） |
| GET | `/tasks/{task_id}/findings` | 该任务发现的漏洞 / 风险点 |
| GET | `/tasks/{task_id}/artifacts` | 该任务的产物（截图 / PoC / 报告 / 日志元数据） |
| GET | `/audit` | 全局结构化审计日志（可按 task / target / action 过滤） |

### 审批（`/api/v1/approvals`）

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/approvals` | 列出本租户审批请求（可按 status 过滤） |
| GET | `/approvals/{approval_id}` | 审批请求详情 |
| POST | `/approvals/{approval_id}/decision` | 批准 / 拒绝（fail-closed） |

### 报告（`/api/v1/tasks`）

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/tasks/{task_id}/report` | 结构化报告（JSON） |
| GET | `/tasks/{task_id}/report/markdown` | Markdown 报告导出 |
| GET | `/tasks/{task_id}/report/json` | JSON 报告导出 |

### 系统（`/api/v1/system`）

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/system/worker-status` | ARQ Worker 在线情况与队列积压 |
| POST | `/system/task-reconcile` | 任务状态对账，收敛幽灵任务（取消标志 / 队列丢失 / 超时） |

### 定价（`/api/v1/pricing`）

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/pricing` | 读取全局价格配置（单条 upsert，id 固定 `default`） |
| PUT | `/pricing` | 更新输入 / 输出每百万 token 单价与币种 |

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
├── web/                     # 前端 SPA（FastAPI 直接挂载，无需构建）
│   ├── index.html
│   └── static/
│       ├── css/styles.css
│       └── js/app.js
└── pobi_v2/
    ├── __init__.py
    ├── main.py              # FastAPI 入口（挂载 /static、/app、/web/*）
    ├── core/
    │   ├── config.py        # 配置（pydantic-settings）
    │   ├── exceptions.py    # 统一异常与 HTTP 映射
    │   ├── security.py      # JWT + bcrypt
    │   ├── deps.py          # get_current_user 鉴权依赖
    │   └── seed.py          # 启动幂等 seed admin
    ├── db/
    │   ├── session.py       # engine / session / Base
    │   ├── models.py        # Tenant/User/Target/Task/ApprovalRequest/Finding/AuditEvent/TaskEvent/Artifact/PricingConfig
    │   └── persistence.py   # 落库辅助（事件/发现/审计/产物）
    ├── schemas/
    │   ├── target.py
    │   ├── task.py          # 含 PlanStep / TaskLiveState / TaskInstructionIn / TaskUsage / UsageSummary
    │   ├── persistence.py   # 查询返回 Schema
    │   ├── auth.py          # 用户/租户/令牌 Schema
    │   ├── approval.py      # 审批请求 Schema
    │   └── pricing.py       # 价格配置 Schema
    ├── routers/
    │   ├── auth.py          # 注册/登录/me/租户
    │   ├── targets.py       # 租户隔离
    │   ├── tasks.py         # 任务 CRUD / cancel / live / usage，租户隔离
    │   ├── instruction.py   # 运行指令追加（M8+）
    │   ├── stream.py        # SSE，租户隔离
    │   ├── persistence.py   # findings/events/artifacts/audit，租户隔离
    │   ├── approval.py      # 审批请求列表/决策
    │   ├── report.py        # 报告导出
    │   ├── pricing.py       # LLM 价格配置 GET/PUT
    │   └── system.py        # Worker 状态 + 任务对账（M8+）
    ├── llm/                 # 统一 LLM 抽象层（LiteLLM+Instructor），预留未接入消费方
    │   ├── __init__.py
    │   ├── client.py        # complete / complete_json / chat
    │   ├── config.py        # 多供应商模型规格解析（本地优先）
    │   └── types.py         # ModelSpec / LLMMessage / UsageRecord / LLMError
    └── engine/
        ├── event_bus.py           # 事件总线（对接 pobi_agent EventHooks）+ 会话级 token 累计
        ├── agent_adapter.py       # CoreAgent/PobiAgent 适配层（含审批回调挂载）
        ├── guardrails.py          # 授权范围护栏（复用 scan_tools.build_scope_policy）
        ├── scan_workflow.py       # M7 扫描工作流（复刻 DeadEndAgent 两阶段，Docker 缺失时回退）
        ├── scan_tools.py          # M7 HTTP 侦察(httpx)+受限 shell（复用 ScopePolicy 闸门）
        ├── deadend_runner.py      # M8 完整引擎适配层（驱动原 DeadEndAgent）
        ├── executor.py            # 任务执行管线（驱动 deadend_runner / 落库 / 取消 / 续跑 / token 落库）
        ├── instruction_channel.py # 运行指令通道（与 cancel_state 同构，memory/redis）
        ├── queue.py               # ARQ 队列
        ├── worker.py              # ARQ WorkerSettings
        ├── cancel_state.py        # 取消标志存储（memory/redis）
        ├── approval.py            # M5 审批引擎（判定/决策/回调）
        └── report.py              # M5 结构化报告渲染
```

> **注**：`pobi_agent/`（内嵌 AI 引擎）位于**仓库根目录**，非 `pobi_v2/pobi_v2/` 子包；通过 uv workspace 复用。

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

纯静态前端 SPA 由后端直接托管，**无需 Node / 构建步骤**：

```bash
# 后端启动后直接访问
open http://localhost:8000/app
```

- 登录页：邮箱 / 密码登录，或开放注册（默认开启，生产请置 `POBI_V2_ALLOW_OPEN_REGISTRATION=false`）。
- 主界面左侧切换页面：
  - **任务看板**：新建（校验授权范围 -> 入队）、查看详情、SSE 实时事件流、取消 / 重入队、报告导出；任务控制台内嵌 token 概览卡片。
  - **授权目标**：管理 `in_scope` / `out_of_scope`，护栏据此拒绝越权任务。
  - **审批**：高危工具调用待审批时，可一键批准 / 拒绝（fail-closed）。
  - **审计**：按时间倒序查看全局审计事件。
  - **Token 用量**：全局汇总卡片 + 价格配置 + 任务明细表，按每百万 token 单价估算成本。
- 静态资源挂载于 `/static`，SPA 路由走 `/web/*`（非资源回退 `index.html`）。

> 仅做本地文件打开（`file://`）不可用，必须经过后端 `/app` 以携带同源 Cookie 与 CORS。

### 端到端流程

1. `POST /api/v1/targets` 创建授权目标（填写 `in_scope` / `out_of_scope`）。
2. `POST /api/v1/tasks` 创建任务（自动校验授权范围 -> 入队）。
3. 前端订阅 `GET /api/v1/tasks/{task_id}/stream` 实时查看 Agent 思考与工具调用。
4. Worker 执行完成后，任务状态流转为 `completed` / `failed`，结果写入 `Task.result`。

> 沙箱工具（Docker / Playwright / AVFS）需要相应环境。M8 主路径默认驱动 `DeadEndAgent`，无 Docker 时回退 M7 `ScanWorkflow`（见 `engine/executor.py`）。

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
POBI_V2_CORS_ORIGINS=https://your-domain  # 生产收敛 CORS（dev 放行 *，禁止与 credentials 同用 *）

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

# 一键编排：postgres -> redis -> api -> worker -> web(nginx)
docker compose up -d --build

# 数据库迁移（首启或升级时）
docker compose exec api alembic upgrade head
```

启动顺序依赖：`postgres` / `redis` 健康检查通过 -> `api` / `worker` 启动 -> `web`(nginx) 反代就绪。
访问入口为 `http://<host>/`（nginx 已开启 Gzip 并正确透传 SSE 长连接）。

### 3. 服务说明

| 服务 | 镜像 | 职责 |
|------|------|------|
| `postgres` | `${ACR_REGISTRY}/postgres:16-alpine` | 主数据存储 |
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

## 待办

工程治理与扫描内核优化方向详见 `docs/PROJECT_GOAL.md`：

- **§ 2.4 工程治理待办（A1–A7）**：分层倒置 / `python_scripts/` 失序 / `logs/` 入版本控制 / `llm/` 孤儿模块 / CORS 不安全 / `main.py:web_app` 分支矛盾 / `routers/system.py` 脆弱写法。
- **§ 2.5 扫描内核优化方向（S1–S6）**：侦查产物结构化与向量化 / 上下文按需检索 / 指纹识别能力 / 漏洞利用工具补全 / Supervisor prompt 去靶场假设 / LLM 决策与工具执行分工。
