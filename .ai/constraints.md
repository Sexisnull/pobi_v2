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

## 设计取舍记录

- **[CORS]** dev `allow_origins=["*"]` + `allow_credentials=True` 并存（源码现状），但 `AGENTS.md` 要求生产收敛为具体 origin，且禁止 `*` 与 credentials 同用。生产须改 `POBI_V2_CORS_ORIGINS`。
- **[Dockerfile.prod]** 强制多阶段构建（编译工具与运行时隔离）；`docker-compose.yml` 镜像强制阿里云 ACR 前缀，禁止官方裸镜像（如 `mongo:7.0`，应 `redis:7-alpine` 等 ACR 前缀）。
- **[前端零构建]** 纯静态 SPA，FastAPI 直接挂载 `web/`，不引入 Node/打包；nginx 开启 Gzip 且对 `/api/v1/tasks/` 关闭代理缓冲以保证 SSE 实时。
- **[agent_mode]** `hacker`（默认需审批）/ `yolo`（自动批准高危，授权靶场自动化）；`POBI_V2_AUTO_APPROVE=true` 时审批回调自动批准。
- **[probe 快路径]** `POST /system/probe` 仅 probe 自身绕过 avfs/多智能体（避免 dev 环境 avfs 未挂载卡死），正常任务仍走 M8 完整链路；硬超时 90s。
