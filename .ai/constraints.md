# 硬性约束与红线

> 取自 `AGENTS.md`、`README.md`、项目实际配置。以源码为准，文档滞后即修正。

## 风险模块（谨慎修改）

| 模块/文件 | 风险说明 | 修改前必须 |
|-----------|---------|-----------|
| `pobi_v2/engine/executor.py` | 任务执行管线核心：分流 deadend/probe、落库、协作式取消、子 Agent 子超时、续跑、token 落库 | 完整追溯调用链；确认 cancel_state / ARQ 队列交互 |
| `pobi_v2/engine/deadend_runner.py` | M8 主路径适配层，驱动原 `DeadEndAgent` 完整多智能体 | 理解 `pobi_agent` 上游内核；改动需追溯上游 pobi |
| `pobi_v2/engine/event_bus.py` | 事件总线对接 `EventHooks` + 会话级 token 累计；SSE 数据来源 | 确认不影响 SSE 实时性与落库顺序 |
| `pobi_v2/engine/approval.py` | M5 审批引擎，fail-closed；按独立 `tool_call_id` 建 `ApprovalRequest` | 保持失败关闭语义，禁止默认放行 |
| `pobi_v2/engine/cancel_state.py` | 取消标志存储（memory/redis），协作式取消依据 | 勿破坏「取消后移除 ARQ 幽灵 job」逻辑 |
| `pobi_v2/main.py` | FastAPI 入口 + lifespan（事件钩子/seed/kali 就绪） | 启动顺序：install_event_hooks → persist_event_worker → seed_admin → ensure_shared_kali_ready（fail-fast） |
| `web/static/css/styles.css` 与 `styles-enhancements.css` | 详情页高度链脆弱；enhancements 后加载会覆盖 styles 同名规则（如 `.chat-stream`） | 改同名选择器须两文件一致，避免层叠冲突 |
| `pobi_v2/llm/` | 统一 LLM 抽象层，当前预留未接入消费方 | 接入消费方前勿删 `ModelSpec` 解析入口 |

## 历史踩坑记录

- **[AVFS not mounted]** `DeadEndAgent` 以 `agent_id` 命名空间挂载 memory，子 Agent 须统一用 `memory_session_id` 访问；误用 `session_id`(task_id) 曾累计阻塞 131 次。修复点：`executor.py:113-115/:157`、`pobi_agent.py:438-440`。→ 读写 memory 必须用同一命名空间。
- **[function-call 序列化]** `parse_browser_steps` 兼容模型将 `steps` 字符串化（先 `json.loads` 解包 `{"steps":[...]}`），避免 `'str' object has no attribute 'items'`。
- **[认证死循环]** `wait_for_auth_success` 假阳性修复；`authenticate_service` 增加 `validated` 真实校验 + 连续失败熔断（`_AUTH_FAIL_LIMIT=3` 直接 `aborted`）。来源：监控报告 A/B（DVWA 登录 33 分钟死循环）。
- **[协作式取消残留]** 取消后任务仍 `running`：取消标志写入后主动从 ARQ 移除幽灵 job；子 Agent 执行包 `asyncio.wait_for` 子超时；Worker 每 5 分钟 `task-reconcile` 对账。来源：报告 C。
- **[SSE 终态重连]** 已结束/取消/失败任务（`completed`/`failed`/`cancelled`）打开详情时不应再建立 SSE 或无限重连；`app.js` 启动前判断状态 + `onerror` 判断终态不重连，否则 `ERR_INCOMPLETE_CHUNKED_ENCODING` 反复出现。
- **[前端详情页高度链]** 全局高度链 `console-root → console-body → console-shell → console-grid → col-center` 脆弱；media query `(min-height:761px)` 曾把 `.console-root`/`.console-grid` 设 `overflow:hidden` 导致整页无法滚动。修复：改 `overflow:auto`；对话流 `.chat-stream` 限高 `max-height:60vh; overflow-y:auto` 内部滚动，蜂群区自然拉长。

- **[RECON 同步链路断点（2026-08-27 修复）]** 原设计：worker 进程 `ContextEngine._recon_emit_sync` 经内存事件总线 `emit_recon_upsert` 推 `__recon_sync__` 事件，由 web 进程 `recon_sync_worker` 消费并 `upsert_to_pg` 到 PG 聚合层。但 agent 跑在 worker 进程、默认 `MemoryEventBusBackend` 不跨进程，事件丢失 → `recon_facts_agg`/`recon_threats_agg` 始终为空（历史任务全空）。**修复**：① `_recon_emit_sync` 改为同进程直连 `ReconStore.upsert_to_pg`（经 `asyncio.create_task`，`ContextEngine` 注入 `pg_session_factory`，由 `deadend_runner` 构造 `DeadEndAgent` 时经 `_async_session_factory()` 传入）；② `deadend_runner.run_deadend_agent` 的 `finally`（统一出口 `clear_task_root` 前）显式 `await upsert_to_pg` 兜底 flush，抗中断丢数据；③ 停用 `recon_sync_worker`（`event_bus.py` 标弃用空壳，`main.py` 移除启动）。另修复 `upsert_to_pg` 内 `source_tasks.op("||")` 对 JSON 列非法的 bug（PG 无 `json||json` 操作符），改为冲突时取 `excluded.source_tasks`。**约束**：改 `ContextEngine`/`ReconStore` 同步逻辑时必须保持同进程直连，勿退回跨进程事件总线；`pg_session_factory` 必须注入否则同步静默跳过。

- **[浏览器启动失败 + LLM 死循环（2026-08-28 修复）]** 任务 `535aee8e` 在登录步骤反复重试 `browser_run_steps` 至 30 分钟 job_timeout。根因有两层：① **binary 路径缺失**——`BrowserSession` 未向 Pydoll 注入 `binary_location`，Pydoll 默认只校验系统路径 `/usr/bin/google-chrome`，而 Docker 镜像仅预装 Playwright 自带 chromium（`PLAYWRIGHT_BROWSERS_PATH=0`），系统 chrome 不存在；② **容器内 root 需 `--no-sandbox`**——chromium 在容器内以 root 运行必须 `--no-sandbox`，否则进程立即退出、CDP 端口从不监听，Pydoll `_verify_browser_running` 超时。**另修复两个预存参数 bug**：`verify_ssl=False` 时 `add_argument("ignore-certification-errors")` 缺 `--` 前缀（被当成 URL 位置参数）；默认 user-agent 字符串 `AppleWebKit/537.36(KHTML` 缺空格。**修复**：`browser.py` 新增 `_find_chromium_candidate`（优先完整 chromium、回退 headless-shell/系统路径，模块导入时预探测缓存）+ `BrowserSession` 透传 `binary_location`；`_build_chromium_options` 加 `--no-sandbox`/`--disable-setuid-sandbox`、修正 `--ignore-certificate-errors` 与 user-agent。**约束**：① Pydoll 链路**必须**用完整 chromium（headless-shell 在 Pydoll 下报 `NoValidTabFound`，不保证默认 tab 发现），headless-shell 仅作回退；② 容器内浏览器参数**不得**移除 `--no-sandbox`；③ 用同步/文件扫描探测路径，禁止在 async 调用栈内用 `sync_playwright`（Playwright 明确禁止，且退出时 `TargetClosedError` 会覆盖返回值）。

- **[TaskEvent payload 双层嵌套（2026-08-28 修复）]** 任务详情页日志区出现大量「Iteration ? · 0 messages」「LLM Input · user」（空内容、不可点开详情）的无意义条目。根因：`#unify-event-structure`（commit 914422d）为 SSE 引入 `_wrap` 外层信封 `{type, session_id, payload:{...}}`，但 `persist_event_worker` 落库时仍整体存储 `dict(event)`（减去 type/session_id），导致 DB `TaskEvent.payload` 变成双层 `{"payload": {...}}`；而 `/plan`、`/live`、`/events` 与前端 `eventToChat` 全部按单层读取 `ev.payload.iteration/content/...`，解析不到 → 回放乱码。连带 `/plan` 步骤、`/live` 阶段/智能体/工具聚合全部为空。**修复**：`persist_event_worker` 改存 `event.get("payload") or {}`（单层），与 executor 直落 `agent_result` 口径一致。**约束**：`TaskEvent.payload` 永远只存内层字段（单层）；`_wrap` 信封仅用于事件总线/SSE 线上传输，落库与消费方均按单层处理，禁止再叠加外层。历史任务旧数据仍为双层，需回放/重跑或 SQL 解包回填才可见。

## 目录结构约定（agent 产物路径）

- **[扁平化：去掉 agent_id / session_id 嵌套层]** 任务级 agent 产物统一归口到
  `tasks/<task_id>/agent/<子目录>/`，**不再嵌套 `<agent_id>/<session_id>` 两层**。
  各产物目录现状（2026-08-27 改造后）：
  - `auth_context/`：认证状态（按 target 的 profile 名隔离，复用 `auth_context/index.json`）
  - `run_context/`：运行上下文快照
  - `webpages/`、`_chunks/`：浏览器抓取页面
  - `memory/`：子 Agent 记忆总结
  - `workspace/`：agent 写入产出
  - RAG 索引：`tasks/<task_id>/rag/<embedding_session_id>/`（**保留 session 层**，RAG 按 task 隔离、跨 agent 共享）
- **决策依据**：原 `agent_id` 是单机固定 ID（`Config.get_local_agent_id`，所有任务相同，无隔离价值）；`session_id` 在当下实现中 == task_id，与外层 `tasks/<tid>` 重复。两层均不参与真实隔离/查找（认证按 target 隔离、运行上下文按 task 隔离）。**后续若需多 agent 协作共享认证，仅在 `auth_context` 处重新引入 `agent_id` 层即可。** 涉及改动文件：`auth_resolver.py`、`context_engine.py`、`code_indexer.py`、`pw_requester.py`、`rag/session_manager.py`、`pobi_agent.py`（`_prepare_memory_workspace` / workspace 默认根）。
- **历史兼容（已迁移，2026-08-27）**：原先嵌套在 `tasks/<tid>/agent/<agent_id>/<tid>/...` 的历史任务目录，已于本日统一**上移扁平化**：
  - `535aee8e-...`（`agent/02c1a686.../535aee8e.../{auth_context,memory,run_context,webpages,workspace}` → `agent/{...}`）
  - `bbd4d941-...`（`agent/02c1a686.../bbd4d941.../auth_context` → `agent/auth_context`）
  - 另清理一个遗留空壳目录 `02c1a686-ca84-474e-bb8c-70a1d90eb5a3/`（旧 agent_id 被误当任务目录，仅含一个 metrics.json，无 `agent/` 结构、无 `.db`），已删除。
  - 全量扫描确认：所有 48 个任务下 `agent/` 均无 `<uuid>` 嵌套残留。
  - 新任务全部走扁平路径，无需再兼容旧嵌套。`auth_context/index.json` 若记录旧路径属元数据、下次认证会自动重建，不影响目录结构。JSON 元数据里的 `agent_id`/`session_id` 字段保留（记录身份，不影响目录结构）。

## 设计取舍记录

- **[CORS]** dev `allow_origins=["*"]` + `allow_credentials=True` 并存（源码现状），但 `AGENTS.md` 要求生产收敛为具体 origin，且禁止 `*` 与 credentials 同用。生产须改 `POBI_V2_CORS_ORIGINS`。
- **[Dockerfile.prod]** 强制多阶段构建（编译工具与运行时隔离）；`docker-compose.yml` 镜像强制阿里云 ACR 前缀，禁止官方裸镜像（如 `mongo:7.0`，应 `redis:7-alpine` 等 ACR 前缀）。
- **[前端零构建]** 纯静态 SPA，FastAPI 直接挂载 `web/`，不引入 Node/打包；nginx 开启 Gzip 且对 `/api/v1/tasks/` 关闭代理缓冲以保证 SSE 实时。
- **[agent_mode]** `hacker`（默认需审批）/ `yolo`（自动批准高危，授权靶场自动化）；`POBI_V2_AUTO_APPROVE=true` 时审批回调自动批准。
- **[probe 快路径]** `POST /system/probe` 仅 probe 自身绕过 avfs/多智能体（避免 dev 环境 avfs 未挂载卡死），正常任务仍走 M8 完整链路；硬超时 90s。
