# 系统架构

> 信息取自 `README.md` 目录结构、`docs/PROJECT_GOAL.md` 分层图、`pobi_v2/main.py`。以源码为准。

## 整体架构图

```
浏览器
   └─ React SPA    (webapp/ → Vite 构建 → web/spa/，base=/，/ 路径)
        ├─ 开发：宿主 Vite dev server(:5173, HMR)，不进 Docker，改完即生效
        └─ 生产：nginx 根目录托管 web/spa，/ 即控制台，/app/ 为兼容别名
        └─ FastAPI 网关 (pobi_v2/main.py)
             ├─ routers/         # REST + SSE 接口层（auth/targets/tasks/instruction/stream/persistence/approval/report/system/pricing/api_tokens）
        ├─ engine/          # 任务编排：executor / event_bus / agent_adapter / deadend_runner / probe_runner / queue / worker / reconcile / recon_access / cancel_state / approval / report / guardrails
        ├─ db/              # SQLAlchemy 2.0 异步模型与持久化（models / session / persistence）
        ├─ schemas/         # Pydantic Schema（target/task/persistence/auth/approval/pricing）
        ├─ core/            # config / exceptions / security(JWT+bcrypt) / deps / seed
        ├─ llm/             # 统一 LLM 抽象层（LiteLLM+Instructor），agent 内核与平台统一接入
        └─（无独立 services/ 层）  # PROJECT_GOAL 曾提及的跨域服务（邮件/定价）未单设目录：定价实现在 routers/pricing.py + schemas/pricing.py
   └─ pobi_agent（内核，仓库根目录子包，uv workspace 复用）
        ├─ CoreAgent / DeadEndAgent / EventHooks
        ├─ tools/           # 网络请求 / 浏览器 / 文件 / 沙箱执行
        └─ agents/          # 监督者 + 6 子 Agent（executor/MemoryAgent/scanner/requester/shell/python-interpreter）
   外部依赖（全部为 Docker 容器服务，接入统一网络 pobi_net）：
        ├─ PostgreSQL       # 主库（多租户隔离；Docker 容器，仅内网不映射宿主端口）
        ├─ Redis + ARQ      # 任务队列 + 事件总线 + 取消/指令状态（Docker 容器，仅内网不映射宿主端口）
        └─ Kali 沙箱容器     # 隔离命令执行环境（常驻容器 pobi_kali，docker 运行）
```

## 部署拓扑与网络隔离（全面容器化，2026-08-31 确认）

> **系统整体由 Docker Compose 编排搭建，数据库（PostgreSQL）与缓存（Redis）均为容器服务**，接入统一 bridge 网络 `pobi_net`。api/worker 经 Docker-out-of-Docker（挂载 `/var/run/docker.sock`）复用宿主机 Docker daemon 以连接常驻 Kali 容器。

| 服务 | 镜像 | 端口暴露 | 访问方式 |
|---|---|---|---|
| postgres | postgres:16-alpine | **不映射宿主端口** | 仅 `pobi_net` 内 `postgres:5432` |
| redis | redis:7-alpine | **不映射宿主端口** | 仅 `pobi_net` 内 `redis:6379` |
| kali 沙箱 | xoxruns/sandboxed_kali | 不暴露入站端口 | 常驻容器 `pobi_kali`，api/worker 经 DooD 连接 |
| api | pobi_v2:1.0.0 | `8000:8000` | 对外 HTTP 入口之一 |
| worker | pobi_v2:1.0.0 | 无 HTTP 端口 | 消费 ARQ 队列 |
| web (nginx) | nginx:1.27-alpine | `80:80` | 边界层（Gzip / SSE 缓冲 / HTTPS）；**开发默认不启动**（`profiles: [web]`），前端由宿主 Vite(:5173) 提供 |

- **数据库/缓存默认不对宿主开放端口（安全收敛）**：`docker-compose.yml` 中 postgres/redis 均注释「仅内部网络直连，不映射宿主端口」；api/worker 经服务名 `postgres:5432` / `redis:6379` 在 `pobi_net` 内直连。宿主机如需直连数据库（调试/测试），必须显式 override 映射端口（如 `ports: "5433:5432"`）或经 `POBI_V2_DATABASE_URL` 指向可达 PG。
- **对本地开发/测试的影响**：宿主机跑 pytest 默认**连不上容器内 PG** → 依赖 PG 的集成测试（如 `test_protected_route_requires_auth`、`test_alembic_migration_applies`）默认跳过（`tests/test_auth.py:_pg_reachable` 按 `settings.database_url` 探测可达性）。正确运行姿势：① 在 api 容器内跑 pytest（容器内可连 `postgres:5432`）；② docker-compose override 映射 PG 端口；③ `POBI_TEST_PG_URL` 指向可达 PG。另 `tests/conftest.py` 把应用日志目录重定向到临时目录，规避容器路径 `/app/logs` 导致宿主机无法 import 应用（`POBI_V2_LOG_DIR` 可配置）。
- **数据持久化**：`pobi_v2_pg` / `pobi_v2_redis` 命名卷保存数据；`~/.pobi_v2` 挂载到容器 `/root/.pobi_v2`（`POBI_HOME`，agent 产物落盘跨重启保留）；`./logs` 挂载到 `/app/logs`（api/worker 日志，`POBI_V2_LOG_DIR` 可覆盖）。
- **镜像与发布**：api/worker 镜像强制阿里云 ACR 前缀；支持本地构建（`Dockerfile.prod` 多阶段）与远端 pull 两种模式（`start-prod.sh` / `publish-image.sh`）。

## 目录模块职责

| 目录/模块 | 职责 | 对外暴露 | 禁止事项 |
|-----------|------|---------|---------|
| `pobi_v2/main.py` | FastAPI 入口，挂载 /app（React 产物 web/spa 兼容别名）、/health、/docs，注册 lifespan（事件钩子/seed/admin/kali） | `/app`、`/health`、`/docs`、所有 router | 勿在入口写业务；CORS 生产须收敛（dev 暂 `*`，`allow_credentials=True` 禁止与 `*` 同用） |
| `pobi_v2/routers/` | REST + SSE 接口层，租户隔离 | `/api/v1/*` | 禁止在 router 内直接调内核；经 `engine/` 适配 |
| `pobi_v2/engine/` | 任务执行管线、事件总线、ARQ worker、审批引擎、护栏、报告；`reconcile.py` 对账核心（router 与 Worker cron 统一调用，消除 engine→routers 倒置）；`recon_access.py` router 访问本地 RECON 库的统一入口（router 不直接依赖内核） | 被 router 调用 | 禁止跨模块循环依赖；router 不得直接 import `pobi_agent`（经本层适配） |
| `pobi_v2/db/` | 模型定义、session、落库辅助 | ORM 模型 | 禁止在 model 写业务；持久化逻辑放 `persistence.py` |
| `pobi_v2/core/` | 配置/异常/安全/鉴权依赖/seed | `get_current_user` 等 | 安全密钥走环境变量，禁止硬编码 |
| `pobi_v2/llm/` | 统一 LLM 抽象层（**唯一 litellm 入口**） | `complete/complete_json/chat`、`ModelSpec` 解析、异常归一 | 所有 LLM 调用必须经此层；禁止在别处直连 litellm |
| `pobi_agent/`（根） | AI 内核：CoreAgent / DeadEndAgent / 工具 / 子 Agent | 被 `engine/deadend_runner.agent_adapter` 驱动 | 视为外部内核，改动需追溯上游 pobi；LLM 调用经 `pobi_v2.llm`（函数内惰性 import，避免顶层循环） |
| `web/` | 前端构建产物目录：`spa/`（Vite 产物，`base=/`，nginx 根目录 `web/spa` 直接静态托管，`/` 即控制台；`/app/` 为兼容别名反代到 FastAPI `/app` 路由） | `/`（React 控制台）、`/app`（兼容别名）、`/api/v1/*`（API） | ① React 端改动在 `webapp/`（Vite 工程），`npm run build` 输出到 `web/spa/`，勿手工改 `web/spa/`（构建产物）；② nginx `web` 服务挂载 `./web/spa`（非 `./web`）；③ 开发阶段 web 容器默认不启动（`profiles: [web]`），前端由宿主 Vite dev server(:5173, HMR) 提供，无需 build；④ 新增页面需同步 `webapp/src/App.jsx` 路由与 `Layout.jsx` 导航 |

## 核心数据流

1. `POST /api/v1/tasks` 创建任务 → 护栏校验 scope → 状态 `queued` → 入队 ARQ。
2. ARQ Worker 拉起 `engine/executor.py` → 分流 `deadend_runner`（M8 主路径，驱动 `DeadEndAgent`）或 `probe_runner`（probe 快路径，绕过 avfs/多智能体）。
3. 运行期事件经 `pobi_agent.EventHooks` → `engine/event_bus.py` → 落库 `TaskEvent` + 会话级 token 累计；SSE 经 `routers/stream.py` 实时推送。
   - **Token 实时统计链路（2026-08-28）**：统一层返回 `usage` → `emit_llm_response` 内存累计（executor 任务结束落库用）+ **异步 HINCRBY 写 Redis**（`pobi:usage:{tid}`，TTL 24h，跨 worker 合并的实时真源）→ `llm_response` 事件 payload 附加 `token_usage` 累计值 → SSE 推送前端实时刷新 token 卡片；`GET /tasks/{id}/usage` 对 running/queued 任务**优先读 Redis 实时值**，终态回退 DB。`reset_session_usage` 同时清内存与 Redis。
4. **侦察/利用产物旁路落库**：supervisor 调用 requester/shell/webapp_analyzer（侦察与利用共用同一 `RequesterAgent`，仅提示词不同）后，在 `agents/components/executor.py` 的 `_add_agent_output_to_context` 内调用 `_persist_recon_facts`，解析 agent 输出文本中的端点 / 技术栈，经 `ContextEngine.add_discovered_fact`（落 `recon_facts`）与 `ContextEngine.add_recon_endpoint`（落 `recon_endpoints`）旁路写入本地 SQLite（`~/.pobi_v2/tasks/<task_id>/<task_id>.db`，`ReconStore.for_task` 任务级单一库，非 `recon/` 子目录）。该通道在 agent 运行期随跑随写、异常仅记 warning 不阻断主循环，**任务取消不影响已落库数据**；`ContextEngine.recon_store` 未注入时全部 no-op。正式 `findings`/`task_events` 仍仅在 `_persist_outcome` 的 `completed` 路径写入（取消分支跳过）。
4. 高危工具调用 → `engine/approval.py` 创建 `ApprovalRequest`（checkpoint，失败关闭）→ 前端审批或 `auto_approve`。
5. 完成 → 状态 `completed`/`failed`/`cancelled`，`result` 写入；报告经 `routers/report.py` 导出。
6. SSE 断连 → `GET /api/v1/tasks/{id}/events`（`after_seq` 游标）回放，弥补断连即丢。

## 任务运行时数据流向（本地文件 / 本地 sqlite / 前端推送）

> 全链路代码核验于 2026-08-31（worker → deadend_runner → ContextEngine/ReconStore → 事件总线 → SSE → 前端）。
> 产物统一归口 `~/.pobi_v2/tasks/<task_id>/`，由平台层注入 `storage_context.set_task_root`（ContextVar 协程级隔离）分发给各写入点。
> 按约定：**本地数据库 = 本地 sqlite（任务 recon 库 + RAG 库）**；PG 聚合/回放表属同步与回放用途，不列入本通道。

### 目录布局

```
tasks/<task_id>/
├── scope.<task_id>.yaml / validation.<task_id>.yaml   # 平台层：授权范围 / 验证策略
├── <task_id>.db                    # ★本地任务库（sqlite，ReconStore，WAL+FTS5）
├── agent/
│   ├── run_context/context.txt     # 运行上下文（ContextEngine 持续追加）
│   ├── auth_context/               # 认证会话（<profile>.json、playwright_state.json、index.json、target_session.json）
│   └── <agent_id>/<session_id>/
│       ├── memory/summaries/{agent}.md   # Agent 记忆摘要（authenticator/requester/shell/python_interpreter）
│       ├── workspace/  webpages/         # 工作区 / 网页抓取产物
├── rag/<agent_id>/<session_id>/<target>.db   # ★RAG 索引库（sqlite，code chunks + vectors）
├── logs/
│   ├── <session_key>/requester.jsonl      # requester 每次 HTTP 请求/响应
│   └── python_interpreter.jsonl           # Python 解释器每次执行结果
└── metrics/metrics.json                   # 会话指标（token/tool_calls/agent_calls/时长）
```

### A. 本地文件（过程与调试数据）

| 数据 | 路径 | 写入触发点 |
|---|---|---|
| 授权范围 | `scope.<task_id>.yaml` | 任务创建（平台层） |
| 验证策略 | `validation.<task_id>.yaml` | 任务创建（平台层） |
| 运行上下文 | `agent/run_context/context.txt` | `ContextEngine._append_to_context_file` 持续追加（user input / 各 agent 响应摘要） |
| 认证会话 | `agent/auth_context/` | authenticator 登录成功后 `save_context`（auth_resolver，Playwright storage state） |
| Agent 记忆摘要 | `agent/<agent_id>/<session_id>/memory/summaries/{agent}.md` | executor `_persist_agent_summary`（AVFS memory workspace，须用 `memory_session_id`=agent_id） |
| HTTP 请求/响应 | `logs/<session_key>/requester.jsonl` | requester 工具 `_save_responses_to_file`（pretty JSON 追加） |
| Python 执行结果 | `logs/python_interpreter.jsonl` | python_interpreter 工具 `_save_result_to_file`（追加） |
| 会话指标 | `metrics/metrics.json` | `SessionMetrics.save()` 任务推进/结束时写 |

### B. 本地 sqlite（可查的结构化结果）

**B1 任务 recon 库 `tasks/<task_id>/<task_id>.db`（ReconStore）**
由 `ReconStore.for_task` 创建；`ContextEngine` 旁路写入（`add_discovered_fact`/`record_attempt`/`add_execution` → `_recon_bypass_fact` 等；executor `_persist_recon_facts` 落端点/技术栈；**2026-08-31 起 `pw_send_payload` 工具层经 `add_recon_technique` 实时落尝试足迹**），运行期随跑随写、异常仅 warning 不阻断主循环。

| 表 | 内容 |
|---|---|
| `recon_sessions` | 任务会话元数据（target、objective、status、agent_session），`ensure_session` 幂等 |
| `recon_facts` | 侦察事实（category：endpoint/technology/authentication/misc/credential…，含 confidence/source/sensitivity，FTS5 索引） |
| `recon_endpoints` | 端点（path/method/status_code/auth_required/tech_stack/parameters/discovered_via） |
| `recon_techniques` | 已测技术/尝试足迹（name 含 endpoint 前缀、status、success_count/tested_count/last_result），2026-08-31 起由 `pw_send_payload` 工具层实时写入（成功/失败/connection reset 均记），供主控证据驱动收敛 |
| `recon_threats` | 威胁（CVE、severity、status：suspected/confirmed/exploited） |

> **足迹驱动收敛（2026-08-31）**：`RequesterDeps` 注入 `context`（TYPE_CHECKING）→ `pw_send_payload` 每次请求实时 upsert `recon_techniques`（name 幂等键 `"{endpoint} | {payload摘要} [{sha1:8}]"`）→ 接通 `was_already_attempted` 防重复 + `is_surface_dead(endpoint, threshold=10)` 死路硬护栏（BLOCKED 拒绝）→ supervisor 决策 / requester 委派前注入 `get_failed_footprint_summary()` 摘要。目的：不限攻击轮数，靠证据引导子 agent 在死路上转向（如 UNION 全被 connection reset → 切布尔盲注）。

> **PG 增量同步（2026-08-31）**：本地 `recon_facts`/`recon_endpoints`/`recon_threats` 三表加 `pg_synced_at` 脏标记列（旧库 `_ensure_column` 幂等补列）；四个 upsert 更新已有行时自动置脏。`upsert_to_pg` 只读脏行 → PG upsert → 提交成功打标（失败不打标重试不丢数据）。触发侧 `_recon_emit_sync` per-task in-flight 合并（同步期间新写入标记 pending 补一轮），不再每次写入 create_task。收敛策略：内容字段最新 wins + confidence 取 `GREATEST`，替代原 `confidence>` 整行门控（修复静默过期）。新增 PG 资产聚合表 `recon_endpoints_agg`（迁移 0017），`seed_from_pg` 续扫时灌入本地结构化端点。

**B2 RAG 索引库 `rag/<agent_id>/<session_id>/<target>.db`（sqlite_connector）**
`rag_manager.get_connector` + `batch_insert_code_chunks` 写入网页/代码 chunks + 向量，供 `webapp_code_rag` 语义检索；embedder 缺失时优雅降级引导改用 facts/shell。

### C. 前端推送（SSE 实时流 + 查询接口）

**C1 SSE 实时流 `GET /api/v1/tasks/{id}/stream`（EventSource）**
`PobiV2EventHooks` 将 agent 事件发布到事件总线（Redis pub/sub / memory）→ `routers/stream.py` 订阅推送，前端 `eventToChat` 渲染为聊天气泡：

| 事件类型 | 前端呈现 |
|---|---|
| `agent_start`/`agent_end`/`agent_error` | 智能体启动/完成/失败（Agent 群卡片状态徽章） |
| `agent_thought` | 主控思考 |
| `llm_iteration`/`llm_input`/`llm_response` | LLM 迭代号/输入/响应（可折叠面板，含 thinking_text，带 token_usage 实时累计） |
| `tool_call_start`/`tool_call_end` | 工具调用与返回 |
| `phase_changed` | 阶段流转（reconnaissance 等） |
| `task_status_changed`/`confidence_update`/`validation_result` | 状态/置信度/验证 |
| `plan_step` | 执行计划左栏（不进入聊天流） |
| `report_task_event` | 最终安全评估报告（折叠面板，8000 字符截断） |
| `log` | 内部日志（前端忽略） |

**C2 查询/轮询接口（任务控制台 `openTaskConsole` 并发拉取）**

| 接口 | 展示内容 | 数据源 |
|---|---|---|
| `GET /tasks/{id}` | 任务状态、objective、token 用量 | postgres tasks |
| `GET /tasks/{id}/live` | current_phase/current_agent/各 Agent 状态/recent_events/agent_work | 事件聚合 |
| `GET /tasks/{id}/plan` | 执行计划步骤与进度 | 事件落库 |
| `GET /tasks/{id}/findings` | 发现列表 | postgres findings |
| `GET /tasks/{id}/usage` | 用量（running 读 Redis 实时值，终态回退 DB） | Redis + postgres |
| `GET /tasks/{id}/recon/threats` | 威胁态势条（severity 分桶） | 本地 sqlite |
| `GET /approvals?task_id=` | 待审批数 → 自主度仪表 | postgres |

> 注：`/recon/endpoints`、`/recon/facts`、`/recon/summary`、`/recon/coverage` 等接口已存在且直读本地 sqlite，但当前前端 SPA 仅消费 `/recon/threats`（态势条），其余明细为 API 已备、前端未渲染。

### 同一数据的多路去向（交叉点）

| 原始数据 | → 本地文件 | → 本地 sqlite | → PG 文件沉淀层 | → 前端推送 |
|---|---|---|---|---|
| Agent 事件（思考/LLM/工具/状态） | — | —（落 PG 回放表） | — | ✅ SSE + /live |
| 侦察结论（端点/技术/事实） | run_context/context.txt | ✅ recon_facts/endpoints/techniques | ✅ task_context_agg（context.txt，终态落库） | ✅ threats 态势条 |
| Agent 经验摘要 | ✅ memory/summaries/*.md | — | ✅ task_memory_agg（终态落库） | — |
| 认证会话 | ✅ auth_context/*.json | ✅ recon_facts（authentication） | ❌ 不落库（每次重认证） | ✅ agent 状态事件 |
| HTTP 请求/响应 | ✅ requester.jsonl | 网页内容进 RAG 库 | ✅ task_metrics_agg（rag 索引元数据，终态落库） | ✅ tool_call 事件摘要 |
| Python 执行 | ✅ python_interpreter.jsonl | 结论进 recon_facts | — | ✅ tool_call 事件 |
| 用量/指标 | ✅ metrics.json | — | ✅ task_metrics_agg（终态落库） | ✅ /usage、SSE token 卡 |

### 结论口径

- **本地文件**：日志、运行上下文、认证会话、记忆摘要、metrics、scope/validation——过程与调试数据；
- **本地 sqlite**：`<task_id>.db`（recon 五表）承载侦察/威胁结构化成果，RAG 库承载网页代码索引——可查结果数据；
- **前端推送**：SSE 实时事件流 + 查询接口组合，展示状态、思考、LLM 过程、计划、威胁态势。

### 本地文件沉淀层落库到 PG（历史经验复用，2026-08-31 落地，迁移 0016_artifact_agg）

> 需求背景：旧任务 `535aee8e...`（failed 终止）的本地文件（记忆摘要、运行上下文、metrics、rag 索引）含跨任务复用的高价值经验，但 `deadend_runner` 的 `finally` 兜底 `upsert_to_pg` **仅同步本地 sqlite 的 recon 五表**（`recon_facts_agg`/`recon_threats_agg`），本地文件**完全不进 PG**，新任务无法调用。
> 已落地：把"非认证类本地文件产物"按 `target_id` 聚合进 PG 三张 `task_*_agg` 表（模型见 `pobi_v2/db/recon_models.py`，迁移 `alembic/versions/0016_artifact_agg.py`），新任务启动期（`seed_from_pg` 续扫 Recon 之外）经 `seed_local_artifacts` 也能拉取历史经验，agent 先读再做。

**落库时机决策（关键事实，2026-08-31 确认）**
- 事实前提：**任务本地文件（`.db`、`context.txt`、memory 摘要、`rag/`、`logs/`、`metrics/`）在任务运行期由 agent 持续写入，随任务状态不断更新，直到任务进入终态（completed / failed / cancelled / 中断）才停止变化。**
- 用户决策：**仅终态落库**，不在运行期周期/旁路落库。理由：① 终态时本地文件已定型，落库内容完整、可信，无"半截/进行中"语义问题；② 复用现有 `deadend_runner` 的 `finally` 统一出口，零新增定时/事件/旁路写逻辑；③ 当前架构为单任务串行打同一目标，新任务启动时旧任务必然已终态，复用需求已被满足，周期落库收益不足、复杂度过高。
- 落库点：复用 `deadend_runner.py:349` 的 `finally` 块，在现有 `await store.upsert_to_pg(...)`（363-368 行）**之后、同一 try/finally 兜底内**追加 `sync_local_artifacts_to_pg(...)`，与 recon 同步同源触发、同源失败降级（仅 warning 不阻断出口清理）。

**落库边界（明确排除认证）**
- ✅ 落库：Agent 经验摘要 `agent/memory/summaries/*.md`、运行上下文 `agent/run_context/context.txt`、会话指标 `metrics/metrics.json`、RAG 索引元数据（`rag/` 仅存引用，不存向量二进制）。
- ❌ **不落库**：认证相关 `agent/auth_context/*`（含 `target_session.json`/`playwright_state.json`/`index.json`）——会话有时效且属敏感凭据，**每次任务重新发起认证**，不跨任务复用。

**PG 新增聚合表（与 `recon_facts_agg` 同级，按 `target_id`+`tenant_id` 维度，级联删除挂 targets/tenants）**
| 表 | 字段 | 来源本地文件 | 唯一约束 |
|---|---|---|---|
| `task_memory_agg` | target_id, tenant_id, agent_role(authenticator/requester/shell/python_interpreter/sqli_recon…), summary_text, source_tasks json, sensitivity, first_seen, last_seen | `agent/memory/summaries/<role>.md` | (target_id, agent_role) |
| `task_context_agg` | target_id, tenant_id, content_text, source_tasks json, sensitivity, first_seen, last_seen | `agent/run_context/context.txt` | (target_id) |
| `task_metrics_agg` | target_id, tenant_id, metrics_json, rag_index_ref, source_tasks json, first_seen, last_seen | `metrics/metrics.json` + `rag/` 索引元数据 | (target_id) |

- `sensitivity` 全部标记 `internal`（用户决策：**明文存 internal，靠租户隔离保护**，不额外加密）。
- `source_tasks` 记录贡献来源任务，多任务合并时去重（同 target 同键只保留最新一条，复用 `recon_facts_agg` 的 upsert 模式）。

**落库流程（复用 `deadend_runner` `finally` 出口，仅终态触发一次，已落地）**
1. 现有 `upsert_to_pg` flush 本地 sqlite recon 五表 → `recon_facts_agg`/`recon_threats_agg`（位于 `finally` 内 `deadend_runner.py:363-368`）。
2. 同一次 `finally` 兜底内、`store.close()` 前，新增 `ReconStore.sync_local_artifacts_to_pg(task_root, target_id, tenant_id, task_id, async_session_factory)`（`store.py` 实现）：
   - 读 `agent/memory/summaries/*.md` → upsert `task_memory_agg`（按 agent_role）；
   - 读 `agent/run_context/context.txt` → upsert `task_context_agg`；
   - 读 `metrics/metrics.json` + 扫描 `rag/` 索引元数据 → upsert `task_metrics_agg`；
   - **显式跳过 `agent/auth_context/`**；
   - 异常仅记 warning 不阻断主循环（与现有 recon 旁路同策略），任务取消不影响已落库数据。

**复用流程（新任务启动期，已落地）**
- 现有 `seed_from_pg(target_id)` 续扫 Recon（L0/L1/L2）→ 灌本地 sqlite（`pobi_agent.py:997` 调用）。
- 新增 `ReconStore.seed_local_artifacts(task_root, target_id, tenant_id, async_session_factory)`（`store.py` 实现，`pobi_agent.py:1013` 紧接 seed_from_pg 调用）：从三张 agg 表拉取 → 写回新任务 `agent/memory/summaries/` 与 `agent/run_context/context.txt`（仅当本地尚无内容时写入，避免覆盖新任务自身产出），使 `ContextEngine` 与 `_persist_agent_summary` 启动即读到历史经验。
- RAG 索引因体积大仅存元数据引用，新任务按 `rag_index_ref` 按需重建（或跳过，降级走 facts/shell）。
- agent 首轮 prompt 注入顺序（已成立）：目标上下文 → 历史 recon（含 L2 经验）→ covered_block → **最后才发 Approach 工作指令**，保证先读历史再开工。

**验证口径（已落地）**
- 旧任务 failed 后 `task_memory_agg`/`task_context_agg`/`task_metrics_agg` 该 target 有记录（同 `recon_facts_agg` 校验方式，连 docker PG 查）；
- 新建同目标任务，其 `agent/memory/summaries/` 与 `run_context/context.txt` 含旧任务沉淀；
- `agent/auth_context/` 为空，新任务重新认证；
- `alembic upgrade head` 成功，三表创建且外键/索引正确。

## 模块依赖关系

- `routers` → `engine` → `db` / `pobi_agent`（经 adapter）→ 外部依赖。
- `engine/event_bus` 对接 `pobi_agent.EventHooks`；`agent_adapter` 安装钩子（`main.py` lifespan）。
- `pobi_agent/core_agent` **惰性 import** `pobi_v2.llm`（函数内，规避内核↔平台顶层循环）：所有 LLM 调用（补全/结构化/JSON 提取）统一走统一层；统一层负责模型解析、限流、tenacity 重试（归一后类型）、异常归一（抛内核异常）与 usage 返回。
- 前端单形态：`webapp/`（React，Vite 工程）经 `/api/v1/*` + SSE 交互，`base=/`，生产由 nginx 托管 `web/spa`（`/` 即控制台）。**开发阶段不进 Docker**：宿主 `cd webapp && npm run dev` 起 Vite(:5173, HMR)，`/api` 代理到 `127.0.0.1:8000`（api 容器），改完即时生效，无需 build / 重启容器。React 端 API 封装见 `webapp/src/api.js`。
- 两层持久架构（README「记忆与缓存」）：Cache（`POBI_CACHE_HOME`，全局命名空间）与 Memory（`ROOT_DEADEND_PATH/agents/<agent_id>/<task_id>/memory`，`agent_id` 命名空间）；二者生命周期不同，不可混淆。AVFS 按 `session_id × workspace` 建表，memory 读写必须用同一 `memory_session_id`（历史因误用 `session_id`(task_id) 阻塞 131 次）。
