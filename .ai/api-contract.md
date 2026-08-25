# 接口契约与数据模型

> 取自 `README.md` API 接口表（权威）与 `pobi_v2/db/models.py`。以源码 `routers/` 为准。所有 `/api/v1/*` 除 `/auth` 部分端点外默认需 `Bearer <JWT>` 且按租户隔离。

## 对外 API

### 鉴权 `/api/v1/auth`
- `POST /auth/register` 注册（归属租户 slug，返回 JWT；开放注册模式租户不存在自动创建）
- `POST /auth/login` 邮箱/密码登录返回 JWT
- `GET /auth/me` 当前用户信息
- `POST /auth/tenants` 创建租户

### 目标 `/api/v1/targets`
- `GET /targets` 列出租户下授权目标
- `POST /targets` 创建（`in_scope`/`out_of_scope` 以 JSONB 存储）
- `GET/PUT/DELETE /targets/{target_id}` CRUD

### 任务 `/api/v1/tasks`
- `GET /tasks` 任务列表（含 token 三列）
- `POST /tasks` 创建 → 护栏校验 → `queued` → 入队 ARQ
- `GET /tasks/{task_id}` 详情（含 findings/artifacts/事件计数）
- `POST /tasks/{task_id}/enqueue` 重新入队 pending/failed/cancelled
- `POST /tasks/{task_id}/cancel` 协作式取消运行/排队中任务
- `GET /tasks/{task_id}/stream` **SSE** 实时事件流（思考/工具调用/置信度/状态）
- `GET /tasks/{task_id}/live` 实时态聚合（阶段/智能体/计划/待生效指令/最近事件/各 Agent 工作片段 `agent_work`/`last_event_at`）
- `GET /tasks/{task_id}/events` 运行轨迹回放：`type` 过滤 + `after_seq` 游标分页，`limit` 默认 100，返回 `EventReplay{events,total,next_after_seq}`
- `POST /tasks/{task_id}/instructions` 追加指令（协作式检查点消费注入）
- `GET /tasks/usage/summary` 全部任务 token 汇总
- `GET /tasks/{task_id}/usage` 单任务 token 明细

### 持久化查询 `/api/v1`
- `GET /tasks/{task_id}/findings` 漏洞/风险点
- `GET /tasks/{task_id}/artifacts` 产物（截图/PoC/报告/日志元数据）
- `GET /audit` 全局结构化审计日志（按 task/target/action 过滤）

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

### API Token（PAT）
- `routers/api_tokens.py`（README 提及，路由已注册）；明细见 `docs/SDK_API.md` 与源码

## 前端关键交互契约
- 前端 SPA 入口 `/app`；静态 `/static`；SPA 路由 `/web/{path:path}` 非资源回退 `index.html`。
- 须走后端 `/app` 携带同源 Cookie，禁止 `file://` 直接打开。
- SSE：`EventSource('/api/v1/tasks/{id}/stream')`；终态任务（`completed`/`failed`/`cancelled`）不建立流、`onerror` 不重连。
- 控制台详情页 DOM：`.console-root(fixed) → #console-body → .console-shell(flex col) → [.console-head, .console-sitrep, .console-tokens(可选), .console-grid] → .col-center → [.swarm-section, .console-phase, #chat-stream, .console-input]`。

## 基础数据模型（`pobi_v2/db/models.py`）
Tenant / User / Target / Task / ApprovalRequest / Finding / AuditEvent / TaskEvent / Artifact / PricingConfig / (PAT) ApiToken 等。迁移：`alembic/versions/`。
- `Task.agent_mode`：`hacker`(默认) / `yolo`。
- `Task.model`：任务级覆盖模型（`deadend_runner` 取 `task.model or settings.model`）。
- `Target.scope`：JSONB 存 `in_scope`/`out_of_scope`，驱动 `ScopePolicy` 护栏。

## 入参出参规则
- 统一异常处理：`core/exceptions.py` 注册 HTTP 映射。
- Schema 校验：`schemas/` 下 Pydantic（task 含 `PlanStep`/`TaskLiveState`/`TaskInstructionIn`/`TaskUsage`/`UsageSummary`）。
