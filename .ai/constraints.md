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
| `pobi_v2/llm/` | 统一 LLM 抽象层，agent 内核与平台唯一 litellm 入口 | 新增 LLM 调用必须经 `complete/complete_json/chat`；禁止绕过统一层直连 litellm；异常归一与重试逻辑勿分叉 |

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

- **[取消残留：续跑不清 error/finished_at/cancel_requested（2026-08-28 修复）]** 任务 `535aee8e` 出现 status=running 但 `error="检测到取消请求，自动终止"`、`finished_at` 有值、`cancel_requested=t` 的矛盾状态。根因：`re_enqueue_task`（前端"继续"）仅把 status 改为 queued，**不清**上次终止遗留的 `error`/`finished_at`/`cancel_requested`（DB），也不调 `clear_cancel`（Redis 标志）。Redis 标志由 `run_task` 启动时 `clear_cancel(tid)` 兜底清除（故任务能续跑），但 DB 三字段残留导致 UI 显示矛盾，且 `cancel_requested` 永远为 True 可能误导前端"取消中"状态。**修复**：`re_enqueue_task` 续跑时重置 `error=None / finished_at=None / cancel_requested=False` 并提前 `clear_cancel(task.id)`（消除 queued→running 窗口期被对账误判为已取消的窗口）。**约束**：任何"重新入队/续跑"路径必须同时重置终止残留三字段 + Redis 取消标志，禁止只改 status。

- **[LLM 统一抽象层接入 agent 内核（2026-08-28）]** 硬性架构要求：**所有 LLM 交互必须统一走 `pobi_v2/llm` 抽象层**，agent 内核不再直连 litellm。worker 与 api 为同一镜像（`pobi_v2:1.0.0`），容器内均有完整 `pobi_v2` 代码，故采用**代码层收敛**（core_agent 惰性 import `pobi_v2.llm`），不做 HTTP 切割。**改造**：① `core_agent.py` 移除 litellm/acompletion/instructor 直连与自身 tenacity 重试，`_call_llm_with_retry`/`_extract_structured`/`_extract_structured_manual` 全部改走统一层（`complete`/`complete_json`，消息以 OpenAI 原生 dict 透传，新增 `_build_model_spec` 把内核 model/api_key/base_url 构造成 `ModelSpec`）；② 统一层扩展 `LLMRequest.tools` + `LLMResponse.thinking_content/tool_calls`，`complete` 支持 function calling 与 json_mode；③ **异常归一**：统一层 `_normalize_error` 把 litellm 异常分类为内核 `pobi_agent.core_agent` 异常（ContentPolicy→InvalidRequest / Quota / RateLimit / Auth / ModelNotFound / Connection / InvalidRequest / LLMError），tenacity 重试作用于归一后的可重试类型（RateLimit/Connection），避免函数内包装后重试条件失效；④ core_agent 保留 `_emit_llm_error_event` 补发 CLI 可见错误事件。**约束**：① 禁止在 pobi_agent 任何位置直连 litellm（embedding 的 `aembedding` 除外，`models/registry.py`）；② core_agent 对 `pobi_v2.llm` 必须**函数内惰性 import**（`core_agent/__init__.py` 顶层引 CoreAgent，顶层互相 import 会循环）；③ 平台侧统一层调用方已确认均用宽泛 `except Exception`，异常归一为内核异常不破坏兼容；④ `LLMRequest.messages` 兼容 `LLMMessage` 与 OpenAI 原生 dict 两种输入。

- **[子 agent 死循环熔断 + 足迹驱动收敛（2026-08-31）]** 任务 `535aee8e` 中 requester 死磕 `UNION SELECT`，被目标连接重置（connection reset）拦截后不断换 payload 变体，**子 agent 内部**迭代 50 轮不收敛，最终被 `_AGENT_SUB_TIMEOUT`（30min）熔断 `failed`（烧 1600 万 token 零产出）。**根因**：supervisor 提示词"每端点最多 2 次调用"只约束**委派层**，管不到子 agent（requester）内部 LLM 循环；且 `asyncio.TimeoutError` 的 `str()` 为空，熔断后 `tasks.error` 落库空字符串，事后无法定位。**方案**：不限攻击轮数（保住绕过能力），改"尝试留痕 + 主控证据驱动收敛"。**实现**：① `RequesterDeps` 注入 `context`（TYPE_CHECKING 防循环依赖）→ `pw_send_payload` 每次请求实时 upsert `recon_techniques`（**name 必须含 endpoint 前缀** `"{endpoint} | {payload摘要} [{sha1:8}]"`，同 payload 幂等合并计数，成功/失败/connection reset 均记）；② 接通 `was_already_attempted` 防重复（同 payload 已失败即提示换变体）；③ 新增 `ContextEngine.is_surface_dead(endpoint, threshold=10)` **死路硬护栏**：同攻击面失败 >=10 且无成功 → `pw_send_payload` 直接返回 BLOCKED 拒绝继续；④ 新增 `ReconStore.list_techniques` + `ContextEngine.get_failed_footprint_summary`，supervisor 决策与 requester 委派前注入 "Failed Attempt Footprints" 摘要，prompt 规则：失败 >=5 且无新信息视为死路转向；⑤ `pobi_v2/engine/executor.py` 超时分支显式 `raise TimeoutError("DeadEndAgent 子超时熔断…")` 补错误描述。**约束**：① 工具层足迹写入必须旁路 no-op（context 未注入时安全跳过，异常仅 warning 不阻断请求）；② `recon_techniques` 的 name 前缀必须带 endpoint，否则主控无法按攻击面聚合；③ 死路硬护栏阈值默认 10，防止误伤正常多轮绕过。

- **[PG 增量同步 + 资产聚合表（2026-08-31）]** 原 `upsert_to_pg` 每次被旁路写入触发时**全量**读本地 recon 五表全量 upsert，任务高频写入时 create_task 并发堆积有性能压力；且 `recon_endpoints`（端点/资产）从未进 PG，目标全景图缺资产视图。**改造**：① 本地 `recon_facts`/`recon_endpoints`/`recon_threats` 三表加 `pg_synced_at` 脏标记列（NULL=脏），旧库 `_ensure_column` 幂等 `ALTER TABLE ADD COLUMN` 补齐；② 四个 upsert（fact/endpoint/technique/threat）更新已有行时自动置脏；③ `upsert_to_pg` 只读脏行 → PG upsert → **PG 提交成功后**打标（失败不打标，下次重试不丢数据）；④ `_recon_emit_sync` 改 per-task in-flight 合并（同步期间新写入标记 `_recon_sync_pending` 补一轮），不再每次写入 create_task；⑤ 新增 PG 资产聚合表 `recon_endpoints_agg`（`pobi_v2/db/recon_models.py` + alembic `0017_recon_endpoints_agg.py`），`seed_from_pg` 续扫时从该表灌入本地结构化端点；⑥ 新增 `GET /api/v1/targets/{id}/assets` 返回目标资产清单。**顺带修复收敛 bug**：原 `on_conflict_do_update` 用 `where=excluded.confidence > current` 整行门控，导致同键内容更新但置信度未提升时 PG 静默过期；改为**内容字段最新 wins + confidence 取 `GREATEST(旧,新)`**。**约束**：① 任何"更新已有行"的写入路径必须置脏 `pg_synced_at=NULL`，否则增量同步漏更新；② 打标必须在 PG 提交成功后执行（失败不打标，靠脏标记重试）；③ 新本地库建表含 `pg_synced_at`，旧库依赖 `_ensure_column` 幂等补列，禁止删该列。

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
- **[nginx 反向代理]** 当前架构保留 `web`（nginx）作为边界层，价值与取舍如下：
  - **保留理由**：① 强制 Gzip（uvicorn 默认不压缩，批量任务大 JSON 直推前端会明显变慢）；② SSE 长连接统一在反代层关闭代理缓冲（`proxy_buffering off` / `read_timeout 3600s`），`/api/v1/tasks/{id}/stream` 实时性更有保障；③ 统一 80 端口暴露，前端同源走 `/api` 前缀无需跨端口；④ 对外发布时可在此层加 HTTPS/限流而无需改应用代码。
  - **代价 / 风险**：开源版 nginx 对 `upstream` 内域名**只在启动时解析一次并永久缓存**；api 容器重启后 IP 漂移，nginx 仍连旧地址（撞到其它容器 8000 端口）会稳定 502。已用 `resolver 127.0.0.11 valid=10s ipv6=off` + 变量形式 `proxy_pass http://$api_upstream`（无尾斜杠，避免吞 URI）根治，见 `nginx.conf` / `nginx.dev.conf`。
  - **本地开发可行性**：单人本地开发可去掉 nginx，前端 API base 直连 `http://127.0.0.1:8000`，少一层故障面；仅在需要 Gzip / SSE 缓冲控制 / HTTPS / 公网暴露时再用 nginx。这是**可选优化**，非强制。
- **[agent_mode]** `hacker`（默认需审批）/ `yolo`（自动批准高危，授权靶场自动化）；`POBI_V2_AUTO_APPROVE=true` 时审批回调自动批准。
- **[probe 快路径]** `POST /system/probe` 仅 probe 自身绕过 avfs/多智能体（避免 dev 环境 avfs 未挂载卡死），正常任务仍走 M8 完整链路；硬超时 90s。
- **[Token 实时展示：Redis 实时真源 + SSE 增量推送（2026-08-28）]** 前端要求实时展示任务 token 用量。核心约束：token 累计在 **worker 进程内存**（`_session_usage_store`），而 SSE 推送 / `GET /tasks/{id}/usage` 在 **api 进程**，api 读不到 worker 内存，必须经共享存储跨进程。**方案**：① `_accumulate_session_usage` 内存累计（executor 落库用）+ **异步 HINCRBY 写 Redis**（`pobi:usage:{tid}` 三字段，TTL 24h 兜底，多 worker 副本可正确合并）；② `emit_llm_response` 事件 payload 附加 `token_usage` 累计值，随 SSE 推送，前端 `onEvent` 收到 `llm_response` 即增量刷新 `.console-tokens` 卡片（零轮询）；③ `GET /tasks/{id}/usage` 对 **running/queued 优先读 Redis**，终态/无实时数据回退 DB；④ token 卡片显示条件放宽为 `ut>0 || running/queued`（运行中即使 0 也显示，等首条 SSE 刷新）。**约束**：① 不要用统一层进程内累计器替代 Redis——跨进程、多 worker 合并必须靠 Redis，DB 落库保留（历史/汇总页）；② `reset_session_usage`（任务启动）必须**同时清内存与 Redis**，避免跨任务污染；③ 事件总线为 **memory 后端（开发模式）时 `get_realtime_usage` 自动回退内存**，不阻塞；④ **dev 模式热重载约定（docker-compose.override.yml 自动合并生效）**：api/worker 均**挂载宿主机源码**进容器（`./pobi_v2`、`./pobi_agent`、`./pobi_prompts`、`./alembic`），改代码**无需重建镜像**。api 用 `uvicorn --reload` **改码实时生效**；worker 用 `arq`（**故意不用 --watch**，曾致探针任务被 cancel/假活离线），改码后须**手动 `docker compose restart worker`**；web 挂载 `./web` + `nginx.dev.conf`（关静态缓存）即时生效。**禁止**在 dev 下 `docker compose build`（多余；生产用 `start-prod.sh`/`-f docker-compose.yml` 才需要 build）。
- **[nginx 静态 DNS 缓存 → 502（2026-08-31 修复）]** 登录 `/api/v1/auth/login` 稳定返回 `502 Bad Gateway`，nginx 报 `connect() failed (111: Connection refused) upstream: http://172.18.0.5:8000`。根因：`upstream { server api:8000; }` 写法下开源版 nginx 仅在启动时解析一次 DNS 并永久缓存；api 容器重启后 IP 从 `172.18.0.5` 漂到 `172.18.0.6`，而 `.5` 已被 worker 容器占用（worker 不监听 8000），nginx 仍连旧地址 → 撞 worker 的 8000 被拒。**诊断要点**：① 服务本身健康（api 容器内 `Uvicorn running on 0.0.0.0:8000` + `Application startup complete`，且 `docker exec web curl api:8000` 能回 404）；② `docker network inspect pobi_net` 比对 nginx 报错里的 upstream IP 与 api 当前 IP，不一致即此问题。**修复**：`nginx.conf` / `nginx.dev.conf` 的 `upstream` 块改为 `resolver 127.0.0.11 valid=10s ipv6=off` + 变量型 `set $api_upstream "api:8000"; proxy_pass http://$api_upstream;`（变量形式才走 resolver 动态解析，且**不能带尾斜杠**否则吞掉原始 URI），然后 `docker compose up -d --force-recreate web` 生效。**约束**：此后若要加新反代 location，一律复用 `$api_upstream` 变量，勿再新建 `upstream { server ... }` 块（否则重蹈静态缓存覆辙）。


