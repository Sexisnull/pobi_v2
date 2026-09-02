# 接口契约与数据模型

> **唯一真源为 `pobi_v2/routers/` 源码**（`README.md` 的接口表仅作索引，滞后时以源码为准并回改文档）。数据模型见 `pobi_v2/db/models.py` 与 `pobi_v2/db/recon_models.py`。
> 所有 `/api/v1/*` 除 `/auth` 部分端点外默认需 `Bearer <JWT>` 且按租户隔离。
> 本文件于 2026-09-01 依源码全量校对，端点清单以实际 `@router.*` 装饰器为准。

## 对外 API

### 鉴权 `/api/v1/auth`（实测 3 个端点）
- `POST /auth/login` 邮箱/密码登录返回 JWT
- `GET /auth/me` 当前用户信息
- `POST /auth/tenants` 创建租户

> **无注册端点**：`routers/auth.py` 未实现 `/auth/register`，账号创建不经 HTTP 开放入口。

### 目标 `/api/v1/targets`
- `GET /targets` 列出租户下授权目标
- `POST /targets` 创建（`in_scope`/`out_of_scope` 以 JSONB 存储）
- `GET/PATCH/DELETE /targets/{target_id}` CRUD
- `GET /targets/{target_id}/assets` 目标资产/端点全景（`recon_endpoints_agg`，limit 1000）

### 目标总览 `/api/v1/targets/{target_id}`（per-target 跨任务全阶段只读聚合）
> 前端「授权目标 → 目标总览」标签页数据源。全部接口首行校验
> `Target.tenant_id == user.tenant_id`，越权返回 **404**（`NotFoundError`），scope 为 `targets:read`。

- `GET /targets/{target_id}/overview` → `TargetOverviewSummaryOut`
  `{facts_count, endpoints_count, threats_count, findings_count, tasks_count, severity_max, last_seen}`
  - `severity_max` = max(威胁 severity 等级, finding severity 等级)，等级序 `critical>high>medium>low>info`
  - `last_seen` = max(facts/threats/endpoints 的 `last_seen`，findings/artifacts/tasks 的 `created_at`)，无数据为 `null`
- `GET /targets/{target_id}/tree` → `ReconTreeOut{hosts[],total}`
  hosts 按 host 分组（升序），叶子 `ReconTreeNode{path,method,status_code,auth_required,tech_stack,threat_severity_max,threat_confidence}`
  - `threat_severity_max`/`threat_confidence`：按 `path_normalized` 匹配 `ReconThreatAgg.target_endpoint`
    得出，**优先全等**，无命中且 path 长度 > 1 时回退子串包含匹配（根路径 `/` 只参与全等，避免过宽泛）；
    命中多条取最高 severity，同级取最大 confidence；无命中为 `info`/`0.0`
  - 端点上限 500 条（`_MAX_ROWS`，`host + path_normalized` 排序）
- `GET /targets/{target_id}/facts` → `{target_id,total,items[]}` 侦察事实，`category + key` 排序
- `GET /targets/{target_id}/threats` → `{target_id,total,items[]}` 威胁，`confidence` 降序
- `GET /targets/{target_id}/findings` → `{target_id,total,items[]}` 利用验证结果，`created_at` 降序
- `GET /targets/{target_id}/artifacts` → `{target_id,total,items[]}` 产物，`created_at` 降序
- 列表类接口 `limit` 默认 200、`ge=1`、`le=500`；空数据返回空数组而非 5xx

**隔离约定**：`recon_*_agg` 表自带 `tenant_id`，`findings`/`artifacts`/`tasks` 自带 `target_id`
（`Artifact.target_id` 可空）。因接口首行已校验 `Target` 归属，查询只走 `WHERE target_id = :tid`，
**不 JOIN tasks 补 `tenant_id`**。

### 任务 `/api/v1/tasks`（`routers/tasks.py`）
- `GET /tasks` 任务列表（含 token 三列）
- `POST /tasks` 创建 → 护栏校验 → `queued` → 入队 ARQ
- `GET /tasks/{task_id}` 详情（含 findings/artifacts/事件计数）
- `PATCH /tasks/{task_id}` 更新任务（`TaskRead`）
- `DELETE /tasks/{task_id}` 删除任务（同步清理本地产物目录）
- `POST /tasks/{task_id}/enqueue` 重新入队 pending/failed/cancelled
- `POST /tasks/{task_id}/cancel` 协作式取消运行/排队中任务
- `GET /tasks/{task_id}/stream` **SSE** 实时事件流（思考/工具调用/置信度/状态）
- `GET /tasks/{task_id}/live` 实时态聚合（阶段/智能体/计划/待生效指令/最近事件/各 Agent 工作片段 `agent_work`/`last_event_at`）
- `GET /tasks/{task_id}/events` 运行轨迹回放：`type` 过滤 + `after_seq` 游标分页，`limit` 默认 100，返回 `EventReplay{events,total,next_after_seq}`
- `GET /tasks/{task_id}/plan` 执行计划步骤与进度（`PlanSummary`）
- `POST /tasks/{task_id}/instructions` 追加指令（协作式检查点消费注入）
- `GET /tasks/usage/summary` 全部任务 token 汇总
- `GET /tasks/{task_id}/usage` 单任务 token 明细

**本地 RECON 只读查询（直读本地 sqlite，7 个端点）**
- `GET /tasks/{task_id}/recon/summary` / `recon/assets` / `recon/endpoints` / `recon/facts` / `recon/threats` / `recon/threats/{cve_id}` / `recon/coverage`
- 数据源为本地 recon 库（**非 PG**）；当前 React 前端仅消费 `/recon/threats`（态势条），其余为 API 已备、前端未渲染。

### 任务认证前置 `/api/v1/tasks/{task_id}/auth`（PreAuth；**实测仅 2 个端点**）
- `GET /auth/status` 认证状态与已落盘会话情况
- `POST /auth/auto` 触发自动认证（复用任务凭据或 body 覆盖：username/password/login_url）
- 会话落盘：`tasks/<task_id>/agent/auth_context/{profile}.json + {profile}.playwright.json + index.json`（原生 AuthContextHandler 格式，profile 默认 `preauth`）

> **手动登录分支（manual）已搁置（2026-09-01）**
> - 原 `manual/start` / `manual/snapshot` / `manual/action` / `manual/capture` / `manual/abort` 五个端点**已从 `routers/task_auth.py` 移除**；
> - `engine/preauth.py` 的 `ManualAuthSession` 及其注册/获取函数**整体注释保留**（`preauth.py:9` 注明"已禁用，2026-09-01 搁置"）；
> - 前端 `webapp/src/api.js` 对应调用标注 `[DISABLED 2026-09-01]`；`tests/test_preauth.py` 的 manual 用例同被注释；
> - **残留未清理**：`webapp/src/pages/Tasks.jsx:557` 仍有 `auth_mode === 'manual'` 渲染分支（创建表单的 manual 选项已于 `:508` 注释）——清理待办见 `roadmap.md`。

### 创建前凭据预检 `/api/v1/tasks/verify-auth`（2026-09-01 新增，PreAuth）
- `POST /verify-auth` 创建任务前真实登录一次验证凭据（**无 task_id 依赖**，任务未创建）。
  - 入参：`{target_id: UUID, username: str, password: str, login_url?: str, auth_flow?: form|http|json(默认 form)}`；明文密码仅存在于请求体，**不落库、不打日志**。
  - 出参：`{ok, status, valid, message, error?, took_ms}`；`status ∈ success|failed|mfa|aborted|error`，`valid=true` 仅 success；`failed`=凭据错误（前端阻止创建）、`mfa/aborted`=需人工登录（放行创建走手动分支）、`error`=验证异常。
  - 授权：`require_scope("tasks:write")` + `check_scope` 校验目标授权范围；整体超时 45s → 504。
  - 实现：`engine/preauth.verify_credentials` 用一次性临时目录 + 唯一临时 profile（`verify_<uuid8>`）跑 `authenticate_service`，验证结束清理，不落盘会话、不污染熔断计数。

### 持久化查询 `/api/v1`（`routers/persistence.py`）
- `GET /tasks/{task_id}/findings` 漏洞/风险点
- `GET /tasks/{task_id}/artifacts` 产物（截图/PoC/报告/日志元数据）
- `GET /audit` 全局结构化审计日志（按 task/target/action 过滤）

> **⚠ 路由重复（源码事实）**：本模块另注册了 `GET /api/v1/tasks/{task_id}` 与 `GET /api/v1/tasks/{task_id}/events`，与 `routers/tasks.py` 的同名路径**重复**。`main.py` 注册顺序为 `tasks`(:88) **先于** `persistence`(:92)，FastAPI 按注册顺序匹配 ⇒ **这两个端点永不命中，实际生效的是 `tasks.py` 的实现**（前者返回 `TaskDetailRead`，后者为 list 而非 `EventReplay`）。修改此处的同名端点不会生效，勿误改。

### 审批 `/api/v1/approvals`
- `GET /approvals` 本租户审批请求（按 status 过滤）
- `GET /approvals/{approval_id}` 详情
- `POST /approvals/{approval_id}/decision` 批准/拒绝（fail-closed）

### 报告 `/api/v1/tasks`
- `GET /tasks/{task_id}/report` 结构化报告 JSON
- `GET /tasks/{task_id}/report/markdown` Markdown 导出
- `GET /tasks/{task_id}/report/json` JSON 导出

### 系统 `/api/v1/system`
- `GET /system/worker-status` ARQ Worker 在线 + 队列积压
- `GET /system/kali-status` 共享 Kali 沙箱健康
- `GET /system/llm-status` 模型服务连通性
- `POST /system/probe` 端到端链路验证（返回 `task_id`，结果经详情/SSE 拉取）
- `POST /system/task-reconcile` 任务状态对账，收敛幽灵任务

### 定价 `/api/v1/pricing`
- `GET /pricing` 全局价格配置（单条 upsert，id=`default`）
- `PUT /pricing` 更新输入/输出每百万 token 单价与币种

### API Token（PAT）`/api/v1/tokens`（`routers/api_tokens.py`）
- `POST /tokens` 创建（`ApiTokenCreated`，明文仅此一次返回）
- `GET /tokens` 列表（`ApiTokenRead`）
- `POST /tokens/{token_id}/reveal` 再次揭示明文（`ApiTokenReveal`）
- `DELETE /tokens/{token_id}` 删除（204）
- 令牌由 `generate_api_token()` 产出 `(plaintext, prefix, token_hash)`：**明文不落库**，仅存 `token_hash` 与展示用 `prefix`。

## 前端关键交互契约

> 前端为 **React SPA 单形态**（`webapp/` Vite 工程 → 构建产物 `web/spa/`，`base=/`）。`web/` 目录下现**仅有 `spa/`**，旧的零构建版（`web/index.html` + `web/static/`）已从磁盘移除。

- **入口与路由**：入口 `/`（nginx 根目录静态托管 `web/spa`）；`/app` 为兼容别名反代到 FastAPI `/app` 路由；API 前缀 `/api/v1`。
- 页面路由（`webapp/src/App.jsx`，`/` 下均受 `RequireAuth + Layout`）：`/` 总览、`/tasks`、`/tasks/:taskId`、`/targets`、`/targets/:targetId`、`/approvals`、`/audit`、`/usage`、`/tokens`、`/health`；`/login` 受 `GuestOnly` 约束。
- 导航分组（`Layout.jsx`）：运营（总览/任务/授权目标）、治理（高危审批/审计日志）、系统（Token 用量/API 令牌/系统健康）；`/reports`、`/rules`、`/team` 为 `soon` 禁用态（未实现）。
- **SSE 客户端**：`webapp/src/api.js:openTaskStream()` —— **手写流解析而非 `EventSource`**（`api.js:200` 注明原因：需通配监听具名事件）；鉴权 token 走查询参数 `?token=`。终态任务（`completed`/`failed`/`cancelled`）不建立流、`onError` 不重连。
- **线上信封**：`{type, session_id, payload:{...}}`，由 `event_bus._wrap` 生成（`event_bus.py:122`）。
- **落库结构（关键差异，勿混淆）**：`TaskEvent.payload` 只存**内层** `payload`（**单层**），由 `persist_event_worker` 解包；`/plan`、`/live`、`/events` 与前端均按单层读取业务字段（`ev.iteration`、`ev.content`…）。`_wrap` 外层信封**仅用于线上传输**，禁止整体落库（此为 2026-08-28 已修复的双层嵌套 bug）。
- 事件渲染入口：`webapp/src/events.js`（`categoryOf` / `typeLabel` / `describeEvent` / `eventTone`）+ `pages/TaskConsole.jsx`；`plan_step` 仅驱动左栏『执行计划』，不进入聊天流。

## 基础数据模型（`pobi_v2/db/models.py`）
Tenant / User / Target / Task / ApprovalRequest / Finding / AuditEvent / TaskEvent / Artifact / PricingConfig / (PAT) ApiToken 等。迁移：`alembic/versions/`。
- `Task.agent_mode`：`hacker`(默认) / `yolo`。
- `Task.model`：任务级覆盖模型（`deadend_runner` 取 `task.model or settings.model`）。
- `Target.scope`：JSONB 存 `in_scope`/`out_of_scope`，驱动 `ScopePolicy` 护栏。
- `Task` 认证前置字段（迁移 0018_task_auth；**0019_drop_auth_secret 删除 `auth_secret` 列，2026-09-02**）：
  - `auth_mode`：`none`(默认)/`auto`/`manual`
  - `auth_status`：`none`/`pending`/`running`/`success`/`failed`/`mfa`
  - `auth_username` / `auth_login_url` / `auth_profile`(默认 `preauth`) / `auth_error` / `auth_updated_at`
  - **凭据（密码）绝不落库**（pgsql/sqlite 均不存）：仅写入任务目录钱包 `tasks/<task_id>/reusable_credentials.json`（`CredentialsStore.save_credentials`，0600 权限），供 authenticator 重认证消费；`auth_username` 保留 DB 用于展示（非高敏）。
  - `POST /auth/auto` 凭据来源：body `username/password/login_url` 覆盖，否则从任务目录 wallet 回退读取（`CredentialsStore.resolve`，task_root 注入定位）。
  - `auth_mode=auto` 创建任务时后台自动触发认证（`asyncio.create_task`，不阻塞创建响应）。

## 入参出参规则
- 统一异常处理：`core/exceptions.py` 注册 HTTP 映射。
- Schema 校验：`schemas/` 下 Pydantic（task 含 `PlanStep`/`TaskLiveState`/`TaskInstructionIn`/`TaskUsage`/`UsageSummary`）。

## 本地 RECON 旁路落库（运行期）
侦察/利用阶段产物在 agent 运行期旁路写入本地 SQLite（非 PG 主库），供后续任务快速建立认知：
- 路径（任务级单一库）：`~/.pobi_v2/tasks/<task_id>/<task_id>.db`。侦察与利用阶段产物共用此单一库，以不同表（`recon_sessions` / `recon_facts` / `recon_endpoints` / `recon_techniques` / `recon_threats`）区分，不再使用 `recon/` 子目录。
- 触发（被动·输出解析）：`agents/components/executor.py` 的 `_add_agent_output_to_context` → `_persist_recon_facts`，解析 agent 输出的 `detailed_summary`/`thoughts` 文本中的端点（扩展名路径）与技术栈词表。
- 触发（主动·工具层实时足迹，2026-08-31 起）：`pw_send_payload`（`tools/browser_automation/__init__.py`）每次 HTTP 请求后实时 `ContextEngine.add_recon_technique`（→ `ReconStore.upsert_technique`，落 `recon_techniques`），**成功 / 失败 / connection reset 均记**，供主控证据驱动收敛。
- 写入通道：
  - `ContextEngine.add_discovered_fact`（→ `ReconStore.upsert_fact`，落 `recon_facts`，category=`endpoint`/`technology`/`finding`/`authentication`…）
  - `ContextEngine.add_recon_endpoint`（→ `ReconStore.insert_http_transaction`，写一笔 `recon_http_transactions` 观测，含 host/tech_stack/parameters/auth_required；端点树不再直写，统一由 `derive_endpoints_from_transactions` 从 tx 派生后落 `recon_endpoints`）
  - `ContextEngine.add_recon_technique`（→ `ReconStore.upsert_technique`，落 `recon_techniques`，name 幂等键 `"{endpoint} | {payload摘要} [{sha1:8}]"`）
- 读取（证据驱动收敛，2026-08-31 起）：`ReconStore.list_techniques` → `ContextEngine.get_failed_footprint_summary`（按攻击面聚合失败足迹）/ `ContextEngine.is_surface_dead`（死路判断：同攻击面失败 >=threshold 且无成功）。
- 时序保证：随跑随写、异常仅记 warning 不阻断主循环；`recon_store` 未注入时全 no-op。**任务取消不影响已落库数据**（取消分支跳过的是 PG 正式 `findings`/`task_events`，非本地 recon 库）。
- 幂等：端点以 `task_id + path_normalized` 去重；fact 以 `category + key` 去重；technique 以 `task_id + name` 去重（同 payload 合并计数）。

### recon_techniques 表结构（`pobi_agent/recon/sqlite_models.py` → `ReconTechnique`）

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | PK autoincrement | — |
| `session_id` | FK → `recon_sessions.id`（ondelete CASCADE） | 会话归属 |
| `task_id` | String(64) | 幂等键之一 |
| `name` | String(128) | **幂等键**：`"{endpoint} | {payload摘要} [{sha1:8}]"`；必须含 endpoint 前缀，供主控按攻击面聚合失败足迹 |
| `category` | String(64) | `http`（工具层足迹）/ `technology`（executor 输出解析）/ `execution`（record_attempt 旁路） |
| `status` | String(32) | `untested` / `success` / `failed` 等 |
| `success_count` | Integer | 成功次数累计 |
| `tested_count` | Integer | 尝试次数累计 |
| `last_result` | Text | 最近一次结果摘要（如 connection reset 原因） |
| `confidence` | Float | 默认 0.5 |
| `created_at` / `updated_at` | DateTime | 时间戳 |

- 唯一约束：`uq_recon_techniques_task_name (task_id, name)`；索引 `ix_recon_techniques_task_status (task_id, status)`。
- 写入语义：`upsert_technique` 对已存在行**累加** `success_count`/`tested_count`、覆盖 `last_result`/`status`（`status != "untested"` 时）、`confidence` 取最大值。

### recon_endpoints_agg 表结构（`pobi_v2/db/recon_models.py` → `ReconEndpointAgg`，2026-08-31 新增）

per-target 资产/端点聚合表，支撑目标全景图资产视图。迁移：`alembic/versions/0017_recon_endpoints_agg.py`。

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | Uuid PK | — |
| `target_id` | Uuid FK → targets.id | 聚合维度 |
| `tenant_id` | Uuid FK → tenants.id | 多租户隔离 |
| `host` | String(255) | 主机 |
| `path_normalized` | String(512) | 规范化路径 |
| `method` | String(16) | 默认 GET |
| `status_code` | Integer nullable | 最近状态码（时效字段，最新 wins） |
| `auth_required` | Boolean | 是否需认证 |
| `tech_stack` | JSON list | 技术栈指纹（跨任务合并） |
| `parameters` | JSON list | 参数 |
| `notes` | Text | 备注 |
| `discovered_via` | String(128) | 发现途径 |
| `confidence` | Float | 取历史与新值最大值 |
| `source_tasks` | JSON list | 来源任务（最新任务覆盖） |
| `first_seen` / `last_seen` | DateTime(tz) | 时间 |

- 唯一约束：`uq_recon_endpoints_agg_tgt_host_path_method (target_id, host, path_normalized, method)`。
- 读取接口：`GET /api/v1/targets/{target_id}/assets`（`pobi_v2/routers/targets.py`），返回该目标资产清单，供全景图资产视图。
- 续扫预热：`ReconStore.seed_from_pg` 会从本表灌入结构化端点到本地 `recon_endpoints`（比从 facts 解析更完整）。

### PG 增量同步机制（2026-08-31 起）

- **脏标记列**：本地 `recon_facts` / `recon_endpoints` / `recon_threats` 三表新增 `pg_synced_at`（DateTime，NULL=待同步）。旧库经 `ReconStore._ensure_column` 幂等 `ALTER TABLE` 补齐。
- **置脏**：`upsert_fact` / `upsert_endpoint` / `upsert_technique` / `upsert_threat` 更新已有行时自动 `pg_synced_at=NULL`；新行默认 NULL（脏）。
- **增量搬运**：`ReconStore.upsert_to_pg` 只读 `pg_synced_at IS NULL` 的脏行 → PG upsert → **PG 提交成功后**打标 `pg_synced_at=now`（失败不打标，下次重试，不丢数据）。
- **触发合并**：`ContextEngine._recon_emit_sync` per-task in-flight 合并——同步进行期间的新写入只标记 `_recon_sync_pending`，当前轮结束后立即补一轮；不再每次写入 `create_task`（防并发 upsert 堆积）。
- **收敛策略变更**：facts/threats/endpoints 的 `on_conflict_do_update` 不再用 `confidence >` 整行门控，改为**内容字段最新 wins + confidence 取 `GREATEST(旧,新)`**，修复"同键内容更新但置信度未提升则 PG 静默过期"的 bug。
- **端点派生（2026-09-02）**：`recon_endpoints` 为**派生落点**，不再由 sitemap/fingerprint/运行期直写；统一由 `ReconStore.derive_endpoints_from_transactions` 从 `recon_http_transactions` 按 `(host, path_normalized)` 聚合生成（状态择优 2xx>3xx>4xx>5xx、tech_stack/parameters 并集、auth_required 任一真），`pre_recon` 落库后调用。PG 同步时端点来源即该派生结果。
- **seed-out 折叠（2026-09-02 修复 CardinalityViolation）**：`recon_http_transactions` 为 append-only 流水，同 `(method,url)` 可多条；`upsert_to_pg` 写 PG `recon_http_transactions_agg`（唯一键 `target_id+tenant_id+method+url`）前必须按 `(method,url)` 折叠脏行取最新代表，折叠的全部脏行统一打 `pg_synced_at`，否则同 INSERT 命令内重复冲突键触发 `CardinalityViolation`（取消任务场景必现）。
