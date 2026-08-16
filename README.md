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
- **端到端链路验证（probe 快路径）**：`POST /api/v1/system/probe` 在共享 Kali 沙箱对授权目标做一次轻量连通性探测（`curl` 访问 + 单次 LLM 结论），由 `engine/probe_runner.py` 直接执行，仅 probe 自身**绕过 avfs / 多智能体规划执行**（避免 dev 环境 avfs 未挂载导致初始化卡死）；正常渗透任务仍走 M8 `DeadEndAgent` 完整多智能体链路。硬超时 90s 由 `asyncio.wait_for` 保证；前端「健康检查」页可手动触发并展示结果。
- **持久化与产物落库**：运行轨迹 / 发现 / 审计 / 产物入库，支持取消与续跑。
- **事件可观测性与回放**：运行期每类事件（思考 / 工具调用 / LLM 流 / 置信度 / 验证结果 / 状态流转 / 日志）实时落库，SSE 断连后可通过 `GET /api/v1/tasks/{id}/events` 按类型过滤 + `seq` 游标分页回放，控制台时间线可完整回看。
- **认证可靠性**：认证流程新增真实结果校验（不再以「无显式失败」误判成功），并按 `target+profile` 维度实现连续失败熔断（达 `_AUTH_FAIL_LIMIT=3` 直接返回 `aborted`，规避带 CSRF 表单登录等框架能力缺陷导致的无限重试 / token 耗尽）。
- **浏览器 `extract` 步骤**：`browser_run_steps` 的 `steps` 新增 `extract` 动作（`selector` + `context_key` + `attribute∈{value,text,html,checked}`），用于从页面提取动态值（如 CSRF token）回填交互上下文。
- **协作式取消可靠性**：取消标志写入后主动从 ARQ 队列移除幽灵 job；子 Agent 执行包 `asyncio.wait_for` 子超时（不误判取消）；Worker 每 5 分钟自动对账（`task-reconcile`）收敛残留运行态。
- **AVFS 命名空间修复**：`DeadEndAgent` 以 `agent_id` 命名空间挂载 memory 工作区，子 Agent（executor / MemoryAgent）统一以同一 `memory_session_id` 访问，规避「AVFS workspace 'memory' is not mounted」所致的任务阻塞。
- **function-call steps 序列化防御**：`parse_browser_steps` 兼容模型在嵌套 function-call 中将 `steps` 字符串化（先 `json.loads` 解析、解包 `{"steps":[...]}`），避免 `'str' object has no attribute 'items'` 类失败。
- **Token 用量统计**：会话级 token 累计落库，按每百万 token 单价估算成本。
- **前端 SPA**：纯静态单页应用（vanilla JS + SSE），FastAPI 直接挂载 `web/`，零构建步骤。

## 记忆与缓存持久机制

任务运行期产出的中间产物与子 Agent 结论分**两层**独立持久化，二者命名空间与生命周期不同，不可混淆。理解该机制是排查「缓存未落盘 / 子 Agent 读不到前序结论」类问题的基础。

### 两层持久架构

| 层 | 根路径（容器内默认） | AVFS 命名空间 | 内容 | 写入方 |
|----|----------------------|---------------|------|--------|
| **运行时缓存 (Cache)** | `POBI_CACHE_HOME=/root/.pobi_v2/cache` | 全局（无 session） | `logs/*.jsonl`、`metrics/`、`scope.*.yaml`、`traces/` | executor / 工具层 |
| **记忆工作区 (Memory)** | `ROOT_DEADEND_PATH/agents/<agent_id>/<task_id>/memory` | **`agent_id`** | `target_*.md`（侦察目标）、`summaries/*.md`（子 Agent 完成摘要） | supervisor / 子 Agent |

- 两个根均由环境变量覆盖：`POBI_HOME`（默认 `~/.pobi_v2`，容器设为 `/root/.pobi_v2`，并与宿主 `~/.pobi_v2` 挂载对齐以保证跨重启保留）；`POBI_CACHE_HOME`（默认落在 `POBI_HOME/cache`）。定义见 `pobi_agent/constants.py:31-46`。
- 路径拼接由 `DeadEndAgent._prepare_memory_workspace()`（`pobi_agent.py:260-265`）完成：`DEADEND_AGENTS_PATH / <agent_id> / <task_id> / "memory"`。

### Memory 工作区：子 Agent 共享读取的来源

- 挂载：在 `DeadEndAgent.__init__` 中以 `session_id=str(self.agent_id)`、`workspace="memory"` 挂载到 AVFS（`pobi_agent.py:135-139`）。
- 读取：所有子 Agent（MemoryAgent / scanner / requester / shell / python-interpreter 等）通过 `MemoryWorkspaceDeps(session_id=self.memory_session_id)` 统一以 `agent_id` 命名空间访问（`executor.py:322-324`、`:428-429`），故**同一次任务的所有子 Agent 共享同一 memory 命名空间**，前序子 Agent 写入的 `target_*.md` 与 `summaries/*.md` 可被后续子 Agent 读取。
- 写入：`_persist_agent_summary()`（`executor.py:531-538`）在每个子 Agent 完成时以 `summaries/{agent_name}.md`（`append=True`）落盘其确定性摘要，供后续子 Agent 收敛上下文。

### AVFS 命名空间护栏（重要）

- AVFS 按 `session_id` × `workspace` 二维建表（`avfs.mount` → `_session_states`）。`avfs.resolve` 严格按传入 `session_id` 查表，未挂载时**主动抛 `RuntimeError: AVFS workspace 'memory' is not mounted`**（`avfs.py:143-147`）——这是设计正确的护栏，不是缺陷。
- 因此 memory 的读写**必须使用同一命名空间（`agent_id`）**。调用方务必传入 `memory_session_id` 而非 `session_id`（task_id），否则写入点找不到挂载 → 抛 `not mounted` → 该次写入失败回滚。**历史曾累计因此阻塞 131 次**，故 `executor.py:113-115`、`:157` 与 `pobi_agent.py:438-440` 均有显式注释警示。

### 已知坑位与排障

- **`summaries/*.md` 缺失 ≠ 整层缓存失效**：若仅子 Agent 摘要未落盘，通常是 `_persist_agent_summary` 写入点误用 `session_id`(task_id) 而非 `memory_session_id`(agent_id) 所致；此时 cache 层、`target_*.md`、以及所有子 Agent 读取均正常，仅「前序结论链」断裂。修复：将写入点改为 `session_id=self.memory_session_id`（`executor.py:536`）。
- **确认落盘**：任务进行中可检查容器内 `ls -laR /root/.pobi_v2/cache` 与 `/root/.pobi_v2/agents/<agent_id>/<task_id>/`（含 `memory/target_*.md`、`run_context/context.txt`、`auth_context/`）。若时间戳持续刷新，说明 worker 与缓存写入均健康。

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
| M8+ | 运行指令通道 / 系统状态对账 / 统一 LLM 抽象层预留 / Token 用量统计 / 端到端链路验证（probe 快路径 + 健康检查页） | ✅ |

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
| GET | `/tasks/{task_id}/live` | 实时态聚合（当前阶段 / 智能体 / 计划 / 待生效指令 / 最近事件 / 各 Agent 工作片段 `agent_work` / `last_event_at`） |
| GET | `/tasks/{task_id}/events` | 运行轨迹回放：按 `type` 过滤、`after_seq` 游标分页，`limit` 默认 100，返回 `EventReplay{events,total,next_after_seq}`，弥补 SSE 断连即丢 |
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
| GET | `/system/kali-status` | 共享 Kali 沙箱健康情况（容器 / 退出码 / 启动输出） |
| GET | `/system/llm-status` | 模型服务连通性（模型 / 延迟 / 探活回复） |
| POST | `/system/probe` | 端到端链路验证：在共享 Kali 对授权目标做轻量连通性探测，返回 `task_id`，结果经 `GET /api/v1/tasks/{id}` 或 SSE 拉取 |
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
        ├── probe_runner.py        # 端到端链路验证快路径（共享 Kali + 单次 LLM，硬超时 90s；仅 probe 自身绕过 avfs/多智能体，正常任务仍走 DeadEndAgent）
        ├── executor.py            # 任务执行管线（驱动 deadend_runner / probe_runner 分流 / 落库 / 协作式取消 / 子 Agent 子超时 / 续跑 / token 落库）
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
  - **健康检查**：聚合 Worker / Kali 沙箱 / 模型服务三段实时状态，可一键发起端到端链路探测并展示上一次探测结论。
  - **Token 用量**：全局汇总卡片 + 价格配置 + 任务明细表，按每百万 token 单价估算成本。
- 静态资源挂载于 `/static`，SPA 路由走 `/web/*`（非资源回退 `index.html`）。

> 仅做本地文件打开（`file://`）不可用，必须经过后端 `/app` 以携带同源 Cookie 与 CORS。

### 端到端流程

1. `POST /api/v1/targets` 创建授权目标（填写 `in_scope` / `out_of_scope`）。
2. `POST /api/v1/tasks` 创建任务（自动校验授权范围 -> 入队）。
3. 前端订阅 `GET /api/v1/tasks/{task_id}/stream` 实时查看 Agent 思考与工具调用。
4. Worker 执行完成后，任务状态流转为 `completed` / `failed`，结果写入 `Task.result`。

> 沙箱工具（Docker / Playwright / AVFS）需要相应环境。M8 主路径默认驱动 `DeadEndAgent`，无 Docker 时回退 M7 `ScanWorkflow`（见 `engine/executor.py`）。

### 链路健康探测（probe 快路径）

链路验证不依赖多智能体，走轻量快路径：

1. `POST /api/v1/system/probe`（`target_id` 必填）立即返回 `task_id`，任务异步在共享 Kali 沙箱执行 `curl` 连通性探测 + 单次 LLM 结论。
2. Worker 由 `engine/probe_runner.py` 直接驱动（probe 自身绕过 avfs / DeadEndAgent 多智能体链路，仅做一次连通性探测），硬超时 90s 由 `asyncio.wait_for` 保证，通常数秒返回。
3. 结果经 `GET /api/v1/tasks/{task_id}` 或 SSE 拉取（`status=completed` 时 `result` 含结论）。
4. 前端「健康检查」页封装了三段实时状态（Worker / Kali / 模型）与「发起健康探测」按钮，并展示上一次探测结论。

> 生产 `docker-compose.override.yml` 已去掉 Worker 的 `--watch`（文件变动触发重启会卡死初始化），改后端的源码需手动 `docker compose restart worker` 生效。

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
| `worker` | `pobi_v2:1.0.0` | `arq pobi_v2.engine.worker.WorkerSettings`，异步执行扫描（每 5min 自动对账收敛幽灵任务） |
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

### 已收敛的监控遗留项（历史监控报告 A–D）

基于 `scripts/` 下监控数据，以下基础设施阻塞已修复（详见上文「核心能力」）：

- **事件可观测性**：持久化事件类型由 7 类扩至 17 类，`/live` 聚合最近事件，`/events` 支持回放，修复 SSE 断连即丢。
- **认证死循环**：`wait_for_auth_success` 假阳性已修复，`authenticate_service` 增加 `validated` 真实校验与连续失败熔断（报告 A/B：DVWA 登录 33 分钟死循环）。
- **结构性受阻**：提示词新增「结构性受阻」停止类别（框架 / 工具缺陷），避免无效重试。
- **协作式取消**：取消后主动移除 ARQ 队列幽灵 job + 子 Agent 子超时 + Worker 自动对账（报告 C：取消后任务仍处于 running）。
- **AVFS 命名空间**：`memory_session_id` 统一挂载 / 访问命名空间，消除 131 次「memory is not mounted」阻塞（报告 D）。
- **function-call 序列化**：`parse_browser_steps` 兼容字符串化 steps，消除嵌套 function-call 解析失败。

### 模型路由：按场景分流不同大模型（M9 预研）

现状：`Task.model` 已支持任务级覆盖模型（`deadend_runner` 取 `task.model or settings.model`），但内核 `AgentRunner` 仅接收单个 `ModelSpec`，任务内所有子 Agent 共用同一模型。需求：同一任务内，生成 payload 等敏感输出应走**本地无审核模型**，规划 / 推理仍走**云端有审核模型**。

改造路线（4 步）：

- **M9-1 平台配置支持角色前缀**：`pobi_v2/llm/config.py` 的 `get_model_spec` 增加 `role` 参数，按 `POBI_V2_MODEL_{ROLE}` / `POBI_V2_LLM_{ROLE}_API_KEY` / `POBI_V2_LLM_{ROLE}_API_BASE` 解析（如 `PAYLOAD` 角色指向本地无审核端点），缺省回退 `POBI_V2_MODEL`。仍保持单一解析入口。
- **M9-2 引入 `ModelRouter`**：内核新增薄封装 `ModelRouter(default, roles)`，提供 `for_role(name)`；`deadend_runner` 构造 `ModelRouter` 注入 `DeadEndAgent`，替代裸 `ModelSpec`。
- **M9-3 `AgentRunner` 按角色取模型**：`pobi_agent/agents/factory.py` 的 `AgentRunner.__init__` 兼容 `ModelRouter`，实例化 `CoreAgent` 前以 Agent 名称取对应 `ModelSpec`。按 Agent 名字自动分流，改动最小。
- **M9-4 标记 payload 生成角色**：确认内核中产出 payload 的子 Agent（shell / python_interpreter 等），绑定 `payload` 角色；可选补前端"任务可选模型"UI。

约束：所有模型仍经 LiteLLM 统一路由与计费（token 用量真实、价格按内置表估算）；本地模型 `api_base` 走内网，凭证与云端隔离；云端 `ContentPolicyViolationError` 不入本地 payload Agent。
