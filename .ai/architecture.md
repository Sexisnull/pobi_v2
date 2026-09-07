# 系统架构

> 信息取自 `README.md` 目录结构、`docs/PROJECT_GOAL.md` 分层图、`pobi_v2/main.py`。以源码为准。

## 整体架构图

```
浏览器
   └─ React SPA    (webapp/ → Vite 构建 → web/spa/，base=/，/ 路径)
        ├─ 开发：宿主 Vite dev server(:5173, HMR)，不进 Docker，改完即生效
        └─ 生产：nginx 根目录托管 web/spa，/ 即控制台，/app/ 为兼容别名
        └─ FastAPI 网关 (pobi_v2/main.py)
             ├─ routers/         # REST + SSE 接口层（12 个）：auth / targets / tasks / task_auth / instruction / stream / persistence / approval / report / system / pricing / api_tokens
        ├─ engine/          # 任务编排（19 个）：executor / event_bus / agent_adapter / deadend_runner / probe_runner / queue / worker / reconcile / recon_access / cancel_state / approval / report / guardrails / instruction_channel / preauth / pre_recon / scan_tools / scan_workflow / sitemap(katana_runner)
        ├─ db/              # SQLAlchemy 2.0 异步模型与持久化（models / session / persistence / recon_models[PG 聚合表]）
        ├─ schemas/         # Pydantic Schema（8 个）：target / task / persistence / auth / approval / pricing / recon / token
        ├─ core/            # config / exceptions / security(JWT+bcrypt) / deps / seed
        ├─ llm/             # 统一 LLM 抽象层（LiteLLM+Instructor）：__init__ / client / config / types；agent 内核与平台统一接入
        ├─ benchmark/       # TSec Benchmark 评测入口（run_benchmark.py，可选依赖组 `benchmark`），不参与主运行链路
        ├─ sandbox_bootstrap.py  # 沙箱就绪引导（Kali 容器准备）
        └─（无独立 services/ 层）  # 跨域服务未单设目录：定价实现在 routers/pricing.py + schemas/pricing.py
   └─ pobi_agent（内核，仓库根目录子包，uv workspace 复用）
        ├─ pobi_agent.py    # CoreAgent / DeadEndAgent / EventHooks
        ├─ tools/           # 顶层：recon_lookup / web_resource_extractor / webapp_code_rag / shell / tool_wrappers
        │                   # 子包：avfs / browser / browser_automation / fingerprint / sitemap / python_interpreter / webapp_analyzer
        └─ agents/
             ├─ 顶层：supervisor_agent / planner / validator / judge / reporter / exploit_web_agent / recon_threatmodel_agent / factory / architecture
             ├─ components/：executor / planner / validation_strategies
             └─ generic_agents/（6 个）：authenticator_agent / memory_agent / request_agent(requester) / shell_agent / python_interpreter_agent / webapp_analyzer_agent
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
| `pobi_v2/engine/` | 任务执行管线、事件总线、ARQ worker、审批引擎、护栏、报告；`reconcile.py` 对账核心（router 与 Worker cron 统一调用，消除 engine→routers 倒置）；`recon_access.py` router 访问本地 RECON 库的统一入口（router 不直接依赖内核）；`preauth.py` 认证前置服务（2026-09-01 新增：auto 分支复用 `authenticate_service`，manual 分支封装 `BrowserSession` 远程控制，统一写原生 AuthContext 三件套 + recon_facts） | 被 router 调用 | 禁止跨模块循环依赖；router 不得直接 import `pobi_agent`（经本层适配） |
| `pobi_v2/db/` | 模型定义、session、落库辅助 | ORM 模型 | 禁止在 model 写业务；持久化逻辑放 `persistence.py` |
| `pobi_v2/core/` | 配置/异常/安全/鉴权依赖/seed | `get_current_user` 等 | 安全密钥走环境变量，禁止硬编码 |
| `pobi_v2/llm/` | 统一 LLM 抽象层（**唯一 litellm 入口**） | `complete/complete_json/chat`、`ModelSpec` 解析、异常归一 | 所有 LLM 调用必须经此层；禁止在别处直连 litellm |
| `pobi_agent/`（根） | AI 内核：CoreAgent / DeadEndAgent / 工具 / 子 Agent | 被 `engine/deadend_runner.agent_adapter` 驱动 | 视为外部内核，改动需追溯上游 pobi；LLM 调用经 `pobi_v2.llm`（函数内惰性 import，避免顶层循环） |
| `web/` | 前端构建产物目录：`spa/`（Vite 产物，`base=/`，nginx 根目录 `web/spa` 直接静态托管，`/` 即控制台；`/app/` 为兼容别名反代到 FastAPI `/app` 路由） | `/`（React 控制台）、`/app`（兼容别名）、`/api/v1/*`（API） | ① React 端改动在 `webapp/`（Vite 工程），`npm run build` 输出到 `web/spa/`，勿手工改 `web/spa/`（构建产物）；② nginx `web` 服务挂载 `./web/spa`（非 `./web`）；③ 开发阶段 web 容器默认不启动（`profiles: [web]`），前端由宿主 Vite dev server(:5173, HMR) 提供，无需 build；④ 新增页面需同步 `webapp/src/App.jsx` 路由与 `Layout.jsx` 导航 |

## 核心数据流

1. `POST /api/v1/tasks` 创建任务 → 护栏校验 scope → 状态 `queued` → 入队 ARQ。
   - 认证前置（2026-09-01）：`auth_mode=auto` 时后台 `asyncio.create_task` 触发 `preauth.run_auto_auth`（复用 `authenticate_service`），会话落 `tasks/<task_id>/agent/auth_context/{profile}.*`（原生 AuthContextHandler 三件套，profile=`preauth`）；`auth_mode=manual` 由前端经 `routers/task_auth.py` 启动一次性 BrowserSession 人工登录后捕获。结果写 recon_facts（category=authentication）供 L0/L1 注入。
2. ARQ Worker 拉起 `engine/executor.py` → 分流 `deadend_runner`（M8 主路径，驱动 `DeadEndAgent`）或 `probe_runner`（probe 快路径，绕过 avfs/多智能体）。
3. 运行期事件经 `pobi_agent.EventHooks` → `engine/event_bus.py` → 落库 `TaskEvent` + 会话级 token 累计；SSE 经 `routers/stream.py` 实时推送。
   - **Token 实时统计链路（2026-08-28）**：统一层返回 `usage` → `emit_llm_response` 内存累计（executor 任务结束落库用）+ **异步 HINCRBY 写 Redis**（`pobi:usage:{tid}`，TTL 24h，跨 worker 合并的实时真源）→ `llm_response` 事件 payload 附加 `token_usage` 累计值 → SSE 推送前端实时刷新 token 卡片；`GET /tasks/{id}/usage` 对 running/queued 任务**优先读 Redis 实时值**，终态回退 DB。`reset_session_usage` 同时清内存与 Redis。
4. **侦察/利用产物旁路落库**：supervisor 调用 requester/shell/webapp_analyzer（侦察与利用共用同一 `RequesterAgent`，仅提示词不同）后，在 `agents/components/executor.py` 的 `_add_agent_output_to_context` 内调用 `_persist_recon_facts`，解析 agent 输出文本中的端点 / 技术栈，经 `ContextEngine.add_discovered_fact`（落 `recon_facts`）与 `ContextEngine.add_recon_endpoint`（改为落 `recon_http_transactions` 一笔观测，由 `derive_endpoints_from_transactions` 归并到 `recon_endpoints`）旁路写入本地 SQLite（`~/.pobi_v2/tasks/<task_id>/<task_id>.db`，`ReconStore.for_task` 任务级单一库，非 `recon/` 子目录）。该通道在 agent 运行期随跑随写、异常仅记 warning 不阻断主循环，**任务取消不影响已落库数据**；`ContextEngine.recon_store` 未注入时全部 no-op。正式 `findings`/`task_events` 仍仅在 `_persist_outcome` 的 `completed` 路径写入（取消分支跳过）。
   - **requester 事务对齐（2026-09-02）**：`pw_send_payload`（`pobi_agent/tools/browser_automation/__init__.py`）在请求完成后，经 `_persist_http_tx` 从 raw 请求/响应文本解析出 method/url/status_code/headers/body/title/content-type/params 等结构化字段，经 `ContextEngine.add_recon_http_transaction` 落 `recon_http_transactions`（source=`agent:requester`），与 sitemap 同 schema 对齐；非 HTTP 响应（连接错误等）跳过、失败仅记 debug 不阻断请求主流程。
4. 高危工具调用 → `engine/approval.py` 创建 `ApprovalRequest`（checkpoint，失败关闭）→ 前端审批或 `auto_approve`。
5. 完成 → 状态 `completed`/`failed`/`cancelled`，`result` 写入；报告经 `routers/report.py` 导出。
6. SSE 断连 → `GET /api/v1/tasks/{id}/events`（`after_seq` 游标）回放，弥补断连即丢。

## 审计写入链路（2026-09-07 增强，P0+P1）

**单点收敛**：所有审计经 `db/persistence.record_audit` 写入，函数内统一完成
「actor 校验 → 取当前 OTel span 填 `trace_id`/`span_id` → PG advisory lock 串行化 → 计算行哈希」。
`record_audit_safe` 是其 best-effort 包装（提交失败只记日志），用于路由层，避免治理留痕反噬可用性。

```
routers/auth.py 登录/租户 ┐
routers/targets.py CRUD   ├─→ record_audit_safe ─┐
routers/approval.py 决策  ┘                      │
engine/executor.py 生命周期+汇总                  ├─→ record_audit ─→ audit_events（哈希链，append-only）
engine/approval.py 高危闸门（创建/自动批准/决策） │        ↑
engine/event_bus.py 子 Agent 委派（depth>0）      │   core/otel.current_trace_ids
engine/scan_tools.py 越权拦截                    ┘
```

- **人做了什么**（路由层）与**Agent 做了什么**（Worker 执行链路）二分，动作清单见 `api-contract.md` 审计字典。
- **高风险逐条 + 任务汇总**：高风险动作（高危工具、委派、越权）逐条入 `audit_events`；
  任务完成时 `executor._record_run_summary` 追加一条 `agent.run_summary` 汇总（工具调用数 / 高危次数 / token / 耗时），
  避免全量工具调用膨胀审计表。
- **证据留存**：`audit_events.tenant_id` 由 CASCADE 改 SET NULL（迁移 0021），删除租户/任务不再抹除证据；
  `task_events` 仍随任务级联删除（改外键会破坏既有删除流），故高风险证据必须写 `audit_events` 而非仅落事件表。

## 上下文与记忆分层（ContextEngine，2026-09-03 依源码校正）

> 本节用于消除「记忆架构」相关描述与代码的偏差：**L0/L1/L2 分层索引与威胁状态机均已上线**，真正缺失的是「工作记忆窗口 / 落盘摘要的结构化回流 / 生产路径创建威胁」。

### 两级结构

| 层 | 载体 | 职责 |
|---|---|---|
| 结构化层 | `StructuredContext` | 内存事实/执行记录容器，`get_unified_context` 按 SECTION 拼装 |
| 编排层 | `ContextEngine`（`pobi_agent/context/context_engine.py`） | 持有 `structured` + `workflow_context` + `recon_store`，在结构化上下文前部挂载 RECON 分层块 |

### 注入入口与默认预算（实测值）

| 入口 | 默认 | 消费方 |
|---|---|---|
| `StructuredContext.get_unified_context(max_tokens=6000)`（`:635`） | 6000 | 被编排层同名方法包一层 |
| `ContextEngine.get_unified_context(max_tokens=6000)`（`:1088`） | 6000 | executor / validator / planner / router 共用 |
| `ContextEngine.get_executor_context(max_tokens=6000)`（`:461`） | 6000 | 执行器专用 |
| `ContextEngine.get_all_context(max_tokens=8000)`（`:1033`，async） | 8000 | workflow 全文 |
| `ReconStore.build_index_view`（`store.py:692`） | L1=500 / L2=1500 | L0/L1/L2 分层块 |
| `ReconStore.build_baseline_block(task_id, token_budget=2000)`（`store.py:1958`） | 2000 | supervisor 启动注入 |

### SECTION 结构（`StructuredContext.get_unified_context`，`:635-`）

| SECTION | 内容 | 截断语义 |
|---|---|---|
| 1 | Target + Goal（`:642`） | 全量 |
| 2 | FLAG/EXPLOIT FOUND 复现步骤（`:650`） | 全量 |
| 3 | COMPLETE TEST HISTORY（按 endpoint 分组的全部 executions，`:677`） | **无截断**（注释 `exhaustive, no truncation`） |
| 4 | KEY DISCOVERIES（finding/technology/attack_vector/feature，`:708`） | **无截断**（注释 `full text, no truncation`） |
| 5 | IDENTIFIED ENDPOINTS（`:721`） | 全量 |
| 6 | VULNERABILITIES（category=vulnerability 的 facts，按 confidence 标 CONFIRMED/SUSPECTED/POSSIBLE，`:741`） | `response_excerpt` 截 200 字符 |
| 6.5 | AUTHENTICATION STATE（authentication/credential facts，`:755`） | 全量 |
| 7 | AGENT INSIGHTS（`thoughts` 最近 5 条，`:775`） | 取 `summary` 或 `thought[:150]` |
| 8 | NEXT STEPS（最近 5 条 executions 的 `next_steps`，`:785`） | 限 5 条 |

> **SECTION 3/4 是主膨胀源**：随任务推进线性增长且全文入 prompt，`max_tokens` 未作用于这两块。
> **SECTION 7 已提供「最近 5 条 insights」注入**（读内存 `thoughts`），故「摘要注入」并非从零缺失——缺的是落盘摘要与注入之间的**结构化桥梁**（见下）。

### 长期记忆（已上线，勿重复建设）

- `ReconStore.build_index_view` 实现 L0 目标基线 / L1 端点技术栈（≤500 tok）/ L2 可复用利用经验（≤1500 tok），预算常量见 `store.py:61-62`。
- 挂载点：`ContextEngine._build_recon_index_block`（`:1119`）在 `get_unified_context` 内前置拼接 RECON 块（`:1113-1117`），异常仅 warning 跳过。
- 启动期另有 `build_baseline_block`（预算 2000）注入 supervisor prompt，使 supervisor 运行前即持有目标基线。
- 跨任务续扫：`deadend_runner` 在 pre_recon 后、`threat_model` 前调 `seed_from_pg` + `seed_local_artifacts`。

### 摘要回流（已上线，形态为「每轮 LLM 重汇总」）

- `DeadEndAgent._populate_memory_context`（`pobi_agent.py:330`）在 supervisor 执行前用 `MemoryAgent`（`generic_agents/memory_agent.py`，`message_history=[]` 单轮）读 AVFS memory workspace，产出纯文本摘要回灌 `memory_context` 并分发给 executor / shell / requester / webapprecon deps。
- 落盘侧：`executor._persist_agent_summary` 写 `agent/memory/summaries/<agent>.md`。
- **缺口**：摘要只落盘 + 每轮 LLM 重新汇总，**无「最近 K 条结构化摘要直接注入」通道**（无 `add_agent_summary`，`ContextEngine` 无摘要 deque）。

### 威胁闭环（状态机已实现，生产路径未接入）

| 能力 | 位置 | 状态 |
|---|---|---|
| 创建 `upsert_threat` | `store.py:531` | 已实现（写 SQLite + 置 `pg_synced_at` 脏标记） |
| 状态机 `update_threat_status`（suspected→confirmed→exploited→remediated，单向、迁 `exploited` 须带 `evidence_summary`） | `store.py:1628` | 已实现 |
| 公开入口 `record_threat_status`（旁路语义，内部调 `update_threat_status` + `_recon_emit_sync` 触发 PG 同步） | `context_engine.py:1492` | 已实现，**落库** |
| 自动推进 `_promote_threat_on_success`（`record_attempt` 成功时调用） | `context_engine.py:1469` | 已接线，但**仅在 payload/reason 能正则匹配 `CVE-\d{4}-\d{4,7}` 时生效** |
| **创建入口 `ContextEngine._recon_bypass_threat`**（2026-09-07 接入） | `context_engine.py:1509` | 已实现并**已接线**：`_recon_bypass_fact` 写入类别归一为 `vulnerability` 的 fact 时同步 upsert 威胁 |

> **精确结论（2026-09-07 校正）**：
> 1. 状态机本身完备且**落库**（经 `ReconStore` → PG 聚合），并非「只是占位」；
> 2. 创建入口已由侦察旁路接通：`ExploitWebAgent.highly_possible_vulnerabilities`
>    （`architecture.py:611` 落 `category=vulnerability` 的 fact）经 `_recon_bypass_fact`
>    派生威胁，随 `upsert_to_pg` 增量同步进 `recon_threats_agg`，目标页「威胁」标签可消费；
> 3. **身份键约定**：`recon_threats` 幂等键为 `(task_id, cve_id)`，故非 CVE 的自研漏洞
>    退化为「CVE 编号 → 漏洞名」标识（`extract_cve` 取不到时用 `title[:64]`），
>    与 `seed_from_pg` 既有的 `cve_id or title` 身份口径一致；前端 CVE 列经
>    `isCveId` 守卫，非 CVE 编号显示 `—`，不把漏洞名当 CVE 展示；
> 4. 遗留缺口：`_promote_threat_on_success` **只能识别 CVE 编号**，故自研发现的
>    SQLi / XSS / 弱口令等非 CVE 漏洞**无法从 `suspected` 自动推进**（状态机能力已有，
>    缺非 CVE 的定位策略），需另行设计（如按 title 精确匹配）。

### 已知缺口汇总（真缺口）

1. **上下文膨胀**：SECTION 3/4 全量注入；`_add_agent_output_to_context` 把子 agent detailed_summary/proofs 全文塞成 fact；`maybe_summarize_context` 阈值 `200_000`（`:1136`）远超单轮预算，实际不触发。
   - 其中 **`message_history` 跨轮无界**（L3 主膨胀源）已由路径 A（驱动循环 + `window_messages` 窗口化 + `UsageLimits` 刹车）于 2026-09-05 根治；SECTION 3/4 全量注入与 fact 全文（L2 渲染）由正交的 M1 计划治理，不并入路径 A。
2. **无工作记忆窗口**：无 `working_memory`，即时上下文散在 `message_history` / `current_task_log`，无聚焦的「当前 1-3 步」区。
3. **摘要未结构化回流**：内存 `thoughts` 最近 5 条已由 SECTION 7 注入，但落盘摘要（`agent/memory/summaries/*.md`）与注入之间无结构化桥梁——无 `add_agent_summary`、无 decision/outcome/token_cost 结构化字段、无跨轮持久化的摘要队列，摘要消费仍靠每轮 `MemoryAgent` LLM 重汇总（成本随轮次线性增长）。
4. ~~**威胁未落库**~~（2026-09-07 已修复，创建入口接入，见上）。遗留：非 CVE 漏洞的状态自动推进。
5. **响应级去重**：`recon_http_transactions` **刻意无幂等键**（`sqlite_models.py:330` 注释：保留每次请求历史以便对比 katana 403 vs requester 200），去重只能在 PG 聚合层 `recon_http_transactions_agg` 做；**禁止**在原始流水表加 `(uri_template, body_md5)` 唯一索引。

## Supervisor 驱动循环与 message_history 窗口化（路径 A，2026-09-05 落地）

> 治 L3 主膨胀源（`message_history` 跨轮无界 + 无 `usage_limits` 刹车）。背景与取舍见 `.ai/context-explosion-analysis.md` §8（路径 A）。本计划**仅覆盖路径 A**，与已就绪的 M1（治 L2 渲染膨胀）正交。

### 架构契约

- **决策器而非 router**：`SupervisorAgent.output_type = SupervisorDecision`（`action ∈ {call_agent, complete}`、`agent`、`prompt`、`task_achieved`、`confidence_score`、`detailed_summary`、`proofs`）。supervisor 不再持子 agent 工具（`@supervisor.agent.tool` 注册已删除，仅保留只读 `call_recon_lookup`）。
- **驱动循环归属 `executor.execute_supervisor`**：逐轮 `window_messages(history) → supervisor.run(message_history=窗口化, usage_limits=有界) → 解析 SupervisorDecision`；`action==call_agent` 由 `_run_sub_agent`（经 `_ToolCtx` 垫片复用既有 `call_*`）直调子 agent，返回 **compact 结果**（`_format_tool_result_for_supervisor` 限长）追加进 `supervisor_history`；`complete` 产出 `ResultEvent`。
- **唯一裁剪边界（`run()` 之间）**：取 `RunResult.raw_messages`（等价 pydantic-ai `all_messages()`）→ 剥离 system → `window_messages(history, history_max_messages=24, history_max_tokens=6000)` 窗口化后**重传**下一轮 `message_history`；存储列表本身每轮经 `window_messages` 重赋，杜绝跨轮无界累积。
- **子 agent 隔离**：`_run_sub_agent` 每次 `message_history=None`（独立、不回灌 supervisor 历史）+ 有界 `usage_limits`；仅 compact 摘要进 `supervisor_history`，根治「子 agent 全量结果回灌」膨胀链。
- **M-L3b 刹车（零自研）**：`_DEFAULT_SUPERVISOR_LOOP_CONFIG` 经 `SupervisorLoopConfig.usage_limits` 透传 `UsageLimits(request_limit=40)`；框架层 `FallbackAgentResult`（UsageLimitExceeded 被 `AgentRunner` 捕获）触发即终止循环，立即止血。
- **跨 ADaPT 轮持久**：`architecture.py` 外层 `while` 维护 `supervisor_history: list[dict]`，同 node 迭代间传递；换节点时重建空列表（节点间不复用，避免污染）。
- **落库零改动**：`_add_agent_output_to_context` / `_persist_agent_summary` / `_register_auth_facts_in_context` 仍在 `_run_sub_agent` 内调用，findings 无回归。

### 数据流

```
architecture.py ADaPT while ──supervisor_history 持久跨轮──▶ execute_supervisor 驱动循环
   └─ window_messages(history) ─▶ supervisor.run(message_history=窗口化, usage_limits=有界)
        └─ 解析 SupervisorDecision.action
             ├─ call_agent ─▶ _run_sub_agent 直调子 agent ─▶ 落 fact/summary ─▶ compact turn 追加 ─▶ 再窗口化
             └─ complete   ─▶ ResultEvent(confidence/task_achieved/summary/proofs)
   FallbackAgentResult（usage 触顶）─▶ 终止循环
```

## 任务运行时数据流向（本地文件 / 本地 sqlite / 前端推送）

> 全链路代码核验于 2026-08-31（worker → deadend_runner → ContextEngine/ReconStore → 事件总线 → SSE → 前端）。
> 产物统一归口 `~/.pobi_v2/tasks/<task_id>/`，由平台层注入 `storage_context.set_task_root`（ContextVar 协程级隔离）分发给各写入点。
> 按约定：**本地数据库 = 本地 sqlite（任务 recon 库 + RAG 库）**；PG 聚合/回放表属同步与回放用途，不列入本通道。

### 目录布局

```
tasks/<task_id>/
├── scope.<task_id>.yaml / validation.<task_id>.yaml   # 平台层：授权范围 / 验证策略
├── <task_id>.db                    # ★本地任务库（sqlite，ReconStore，WAL+FTS5）
├── agent/                          # ★扁平化：无 <agent_id>/<session_id> 嵌套层（2026-08-27 改造）
│   ├── run_context/context.txt     # 运行上下文（ContextEngine 持续追加）
│   ├── auth_context/               # 认证会话（<profile>.json、<profile>.playwright.json、index.json、target_session.json）
│   ├── memory/summaries/{agent}.md # Agent 记忆摘要（AVFS workspace，非目录层级）
│   ├── workspace/                  # 工作区（agents_storage_root/workspace）
│   └── webpages/                   # 网页抓取产物
├── rag/<embedding_session_id>/<target>.db   # ★RAG 索引库（sqlite，code chunks + vectors）
├── sitemap/katana_raw.jsonl                 # 前置侦查 katana 原始 JSONL（防误过滤复盘）
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
| Agent 记忆摘要 | `agent/memory/summaries/{agent}.md` | executor `_persist_agent_summary`（AVFS memory workspace，须用 `memory_session_id`=agent_id）。**实路径**：`_prepare_memory_workspace`（`pobi_agent.py:311`）拼 `agents_storage_root / "memory"`，`agents_storage_root` 由 `deadend_runner` 注入为 `tasks/<task_id>/agent`（`Config.agents_storage_root` 默认 `None`，禁止回退旧根） |
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
| `recon_fingerprints` | 指纹明细（2026-09-01 新增）：四层完整指纹 + favicon + auth_mode，`pre_recon` 落库；仅存本地，不进 PG |
| `recon_http_transactions` | HTTP 请求/响应事务流水（2026-09-02 新增）：每次请求一条（无幂等键，保留历史可对比），`source` 区分 `sitemap:katana` / `agent:requester`；含 status/headers/body/title/content_type/size/tech_stack/detected_params/forms、auth_used/auth_required、body_blob_ref（>100KB 大 body 外置 blobs/）；为「站点地图页面对齐 katana 与 requester」的数据基座，仅存本地不进 PG |

> **足迹驱动收敛（2026-08-31）**：`RequesterDeps` 注入 `context`（TYPE_CHECKING）→ `pw_send_payload` 每次请求实时 upsert `recon_techniques`（name 幂等键 `"{endpoint} | {payload摘要} [{sha1:8}]"`）→ 接通 `was_already_attempted` 防重复 + `is_surface_dead(endpoint, threshold=10)` 死路硬护栏（BLOCKED 拒绝）→ supervisor 决策 / requester 委派前注入 `get_failed_footprint_summary()` 摘要。目的：不限攻击轮数，靠证据引导子 agent 在死路上转向（如 UNION 全被 connection reset → 切布尔盲注）。

> **前置侦查 pre_recon（2026-09-01 奠基，2026-09-02 收敛数据模型）**：平台层自动通道，任务启动后（executor 主路径，deadend/ScanWorkflow 共用）在智能体侦查前自动执行**指纹识别 + WAF 识别 + 站点地图**。模块：`pobi_v2/engine/pre_recon.py`（编排：phase/tool 事件 + 认证读取 + 引擎 + 落库）+ `pobi_agent/tools/fingerprint/engine.py`（无 ctx 引擎 `run_fingerprint`/`persist_fingerprint`）。**认证感知**：读取 PreAuth 落盘的 `preauth` 会话注入 cookies（`authenticated:<profile>`）；无会话标记 `no_auth_context`/`external_only` 仅外部探测。**落库四通道**：`recon_fingerprints`（独立明细表，完整四层 + favicon + auth_mode）+ `recon_facts(category=technology)` + `recon_techniques(fingerprint|{host})` 足迹防重；技术栈信息保留在 facts（供 L0/L1 与基线块消费），**端点树不再直写**，统一由 pre_recon 落库后 `ReconStore.derive_endpoints_from_transactions` 从 `recon_http_transactions` 派生。

> **Layer1 启动注入（2026-09-02）**：`execute_supervisor` 的 `supervisor_prompt` 在组装时调用 `ReconStore.build_baseline_block`（token 预算 2000，失败仅 warning），由 `recon_http_transactions` 现算指纹/站点总览/端点/认证面组装成「目标侦察基线」块注入 prompt。supervisor 启动即拥有基线，无需先跑工具；运行期仍靠 `build_index_view`（L0/L1）与按需工具（`list_endpoints`/`search_transactions`）补充。站点总览（`build_site_overview`）读时现算，**不落物化表**。**实时流**：`phase_changed(pre_recon)` + `tool_call_start/end(fingerprint)` 经 SSE 推送前端（前端 events.js 已支持渲染）。指纹明细仅存本地库，technology facts 走 PG 聚合层供同目标新任务 seed 复用。

> **站点地图 sitemap（2026-09-02）**：前置侦查第二子阶段（fingerprint 之后），Kali 内执行 katana 构建站点地图。**katana 由用户在自建 Kali 镜像预装打包（本项目不安装/固化）**，代码侧仅做 `katana -version` 健康检查，缺失 → `status="skipped"` 不阻断任务。执行链路：`pre_recon` → `run_sitemap`（`pobi_v2/engine/sitemap/katana_runner.py`，构造命令含 Cookie/自定义 header 认证注入 + `-ef` 静态扩展名过滤 + `-iqp`；复用 `_resolve_auth(preauth)` 会话）→ Kali `execute_command` → JSONL stdout → 原始输出落盘 `tasks/<id>/sitemap/katana_raw.jsonl`（防误过滤复盘）→ `pobi_agent/tools/sitemap/engine.py`（`parse_katana_jsonl`/`should_keep`/`persist_sitemap`）解析去噪 → 落库 `recon_http_transactions`（source=`sitemap:katana`）；**端点树不再双写**，由 `derive_endpoints_from_transactions` 在事务之上派生；足迹 `recon_techniques(sitemap|{host})` 防重（同 host 只跑一次）。**无意义页面三层防线**：① katana 参数层（`-ef`/`-iqp`）；② 落库层 `pobi_agent/utils/urls.py`（静态资源/登出错误噪音/纯分页参数变体过滤，katana 无 -pcs/-fsu/-filter-page-type）；③ agent 复用注入（L1 端点 + `covered_block`）。**katana 不暴露给 agent 工具集**，仅前置侦查平台层调用（对齐 fingerprint 不暴露模式）。

> **PG 增量同步（2026-08-31 奠基，2026-09-02 修复）**：本地 `recon_facts`/`recon_endpoints`/`recon_threats` 三表加 `pg_synced_at` 脏标记列（旧库 `_ensure_column` 幂等补列）；四个 upsert 更新已有行时自动置脏。`upsert_to_pg` 只读脏行 → PG upsert → 提交成功打标（失败不打标重试不丢数据）。**2026-09-02 修复 `CardinalityViolation`**：`recon_http_transactions` 为 append-only 流水（同 `(method,url)` 可多条，保留 katana 403 vs requester 200 历史），`upsert_to_pg` 写 PG 前按 `(method,url)` 折叠脏行取最新代表，折叠的全部脏行统一打 `pg_synced_at`，否则撞 `recon_http_transactions_agg` 唯一键 `(target_id,tenant_id,method,url)` 同命令重复。收敛策略：内容字段最新 wins + confidence 取 `GREATEST`，替代原 `confidence>` 整行门控（修复静默过期）。端点树由 `recon_http_transactions` 派生后写入 `recon_endpoints` 再同步 PG 资产聚合表 `recon_endpoints_agg`（迁移 0017），`seed_from_pg` 续扫时灌入本地结构化端点。

**B2 RAG 索引库 `tasks/<task_id>/rag/<embedding_session_id>/<target>.db`（sqlite_connector）**
`rag_manager.get_connector` + `batch_insert_code_chunks` 写入网页/代码 chunks + 向量，供 `webapp_code_rag` 语义检索；embedder 缺失时优雅降级引导改用 facts/shell。

### C. 前端推送（SSE 实时流 + 查询接口）

**C1 SSE 实时流 `GET /api/v1/tasks/{id}/stream`**
`PobiV2EventHooks` 将 agent 事件发布到事件总线（Redis pub/sub / memory）→ `routers/stream.py` 订阅推送。
前端消费：`webapp/src/api.js:openTaskStream()`（**手写流解析而非 `EventSource`**——需通配监听具名事件；token 走查询参数 `?token=`）→ 由 `webapp/src/events.js` 的 `categoryOf` / `typeLabel` / `describeEvent` / `eventTone` 归一化后，在 `pages/TaskConsole.jsx` 渲染为聊天气泡：

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
| HTTP 请求/响应 | ✅ requester.jsonl + sitemap/katana_raw.jsonl | ✅ recon_http_transactions（结构化，source 区分来源，**响应体分层存储**） | ✅ recon_http_transactions_agg（target 级 url 收敛，终态落库） | ✅ tool_call 事件摘要 |
| Python 执行 | ✅ python_interpreter.jsonl | 结论进 recon_facts | — | ✅ tool_call 事件 |
| 用量/指标 | ✅ metrics.json | — | ✅ task_metrics_agg（终态落库） | ✅ /usage、SSE token 卡 |

### 结论口径

- **本地文件**：日志、运行上下文、认证会话、记忆摘要、metrics、scope/validation——过程与调试数据；
- **本地 sqlite**：`<task_id>.db`（recon 六表，含 recon_http_transactions）承载侦察/威胁结构化成果，RAG 库承载网页代码索引——可查结果数据；
- **前端推送**：SSE 实时事件流 + 查询接口组合，展示状态、思考、LLM 过程、计划、威胁态势。

### 响应体分层存储 + 目标级增量（HTTP 事务，2026-09-02）

> sitemap/requester 共用的事务表 `recon_http_transactions`（本地流水）与 PG 聚合表 `recon_http_transactions_agg`（target 级收敛）语义不同：**本地看当前任务流水（保留每次请求历史）**，**PG 看目标跨任务最新状态（url 维度行收敛）**。站点地图页本地按 task 读流水，跨任务视图按 target 查 PG agg。

**本地分层存储**（`insert_http_transaction`，store.py）：

| 策略 | 触发条件 | response_body | body_compressed |
|---|---|---|---|
| `full` | <100KB（含 json/xml） | 明文全量 | — |
| `compressed` | 100KB–1MB 非 API；**或 json/xml API ≥100KB（全量保留）** | 200 字符预览 | gzip 压缩全量（BLOB） |
| `digest` | >1MB 非 API | 前 2048 字符摘要 | — |

- 常量：`_BLOB_THRESHOLD_BYTES=100KB` / `_COMPRESS_MAX_BYTES=1MB` / `_DIGEST_PREFIX_BYTES=2048` / `_API_CONTENT_TYPE_RE`（json/xml 判定）。
- 读取侧**按需拉取**：`list_http_transactions` 只回骨架（`storage_strategy` + `body_available` 标记），`get_transaction_body(task_id, tx_id)` 解压/取全文，列表不拖大 body。
- 旧库 `_migrate` 幂等补列（`storage_strategy`/`body_compressed`/`pg_synced_at`）。

**目标级增量（PG）**：完成推送 `upsert_to_pg` 读本地脏事务（`pg_synced_at IS NULL`）→ `recon_http_transactions_agg`（唯一键 `target_id+tenant_id+method+url`，ON CONFLICT 只留最新，含分层 body）→ 提交成功后打标；新任务 `seed_from_pg` 拉目标事务骨架灌本地端点树（covered_block / L1 增量提示，避免重复枚举已抓 url）。

### 本地文件沉淀层落库到 PG（历史经验复用，2026-08-31 落地，迁移 0016_artifact_agg）

> 需求背景：旧任务 `535aee8e...`（failed 终止）的本地文件（记忆摘要、运行上下文、metrics、rag 索引）含跨任务复用的高价值经验，但 `deadend_runner` 的 `finally` 兜底 `upsert_to_pg` **仅同步本地 sqlite 的 recon 五表**（`recon_facts_agg`/`recon_threats_agg`），本地文件**完全不进 PG**，新任务无法调用。
> 已落地：把"非认证类本地文件产物"按 `target_id` 聚合进 PG 三张 `task_*_agg` 表（模型见 `pobi_v2/db/recon_models.py`，迁移 `alembic/versions/0016_artifact_agg.py`），新任务启动期（`seed_from_pg` 续扫 Recon 之外）经 `seed_local_artifacts` 也能拉取历史经验，agent 先读再做。

**落库时机决策（关键事实，2026-08-31 确认）**
- 事实前提：**任务本地文件（`.db`、`context.txt`、memory 摘要、`rag/`、`logs/`、`metrics/`）在任务运行期由 agent 持续写入，随任务状态不断更新，直到任务进入终态（completed / failed / cancelled / 中断）才停止变化。**
- 用户决策：**仅终态落库**，不在运行期周期/旁路落库。理由：① 终态时本地文件已定型，落库内容完整、可信，无"半截/进行中"语义问题；② 复用现有 `deadend_runner` 的 `finally` 统一出口，零新增定时/事件/旁路写逻辑；③ 当前架构为单任务串行打同一目标，新任务启动时旧任务必然已终态，复用需求已被满足，周期落库收益不足、复杂度过高。
- 落库点：复用 `deadend_runner.py:349` 的 `finally` 块，在现有 `await store.upsert_to_pg(...)`（363-368 行）**之后、同一 try/finally 兜底内**追加 `sync_local_artifacts_to_pg(...)`，与 recon 同步同源触发、同源失败降级（仅 warning 不阻断出口清理）。

**落库边界（明确排除认证）**
- ✅ 落库：Agent 经验摘要 `agent/memory/summaries/*.md`、运行上下文 `agent/run_context/context.txt`、会话指标 `metrics/metrics.json`、RAG 索引元数据（`rag/` 仅存引用，不存向量二进制）。
- ❌ **不落库**：认证相关 `agent/auth_context/*`（含 `target_session.json`/`playwright_state.json`/`index.json`）与任务凭据钱包 `reusable_credentials.json`——会话/凭据有时效且属敏感凭据（不可复用资产，登录态失效即作废），**每次任务重新发起认证**，不跨任务复用；凭据仅落任务目录文件供 authenticator 重认证消费（2026-09-02 强化：`tasks.auth_secret` 列已删，密码全程不落任何数据库）。

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
- 现有 `seed_from_pg(target_id)` 续扫 Recon（L0/L1/L2）→ 灌本地 sqlite。**2026-09-02 修复触发面**：原仅 ADaPT `pobi_agent.py:start_supervisor` 分支调用，生产主链路（deadend_runner → threat_model → execute_supervisor）从未触发，导致历史沉淀无法续扫；现 `deadend_runner` 在任务启动、pre_recon 完成后、`threat_model` 前调用 `seed_from_pg` + `seed_local_artifacts` 预热（失败仅 warning），与 ADaPT 分支对齐。
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
