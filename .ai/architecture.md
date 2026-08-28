# 系统架构

> 信息取自 `README.md` 目录结构、`docs/PROJECT_GOAL.md` 分层图、`pobi_v2/main.py`。以源码为准。

## 整体架构图

```
浏览器 (原生 JS SPA, /app)
   └─ FastAPI 网关 (pobi_v2/main.py)
        ├─ routers/         # REST + SSE 接口层（auth/targets/tasks/instruction/stream/persistence/approval/report/system/pricing/api_tokens）
        ├─ engine/          # 任务编排：executor / event_bus / agent_adapter / deadend_runner / probe_runner / queue / worker / cancel_state / approval / report / guardrails
        ├─ db/              # SQLAlchemy 2.0 异步模型与持久化（models / session / persistence）
        ├─ schemas/         # Pydantic Schema（target/task/persistence/auth/approval/pricing）
        ├─ core/            # config / exceptions / security(JWT+bcrypt) / deps / seed
        ├─ llm/             # 统一 LLM 抽象层（LiteLLM+Instructor），预留未接入消费方
        └─ services/        # 跨域服务（邮件、定价等，PROJECT_GOAL 提及）
   └─ pobi_agent（内核，仓库根目录子包，uv workspace 复用）
        ├─ CoreAgent / DeadEndAgent / EventHooks
        ├─ tools/           # 网络请求 / 浏览器 / 文件 / 沙箱执行
        └─ agents/          # 监督者 + 6 子 Agent（executor/MemoryAgent/scanner/requester/shell/python-interpreter）
   外部依赖：
        ├─ PostgreSQL       # 主库（多租户隔离）
        ├─ Redis + ARQ      # 任务队列 + 事件总线 + 取消/指令状态
        └─ Kali 沙箱容器     # 隔离命令执行环境（docker 运行）
```

## 目录模块职责

| 目录/模块 | 职责 | 对外暴露 | 禁止事项 |
|-----------|------|---------|---------|
| `pobi_v2/main.py` | FastAPI 入口，挂载 /static、/app、/web/*，注册 lifespan（事件钩子/seed/admin/kali） | `/app`、`/health`、`/docs`、所有 router | 勿在入口写业务；CORS 生产须收敛（dev 暂 `*`，`allow_credentials=True` 禁止与 `*` 同用） |
| `pobi_v2/routers/` | REST + SSE 接口层，租户隔离 | `/api/v1/*` | 禁止在 router 内直接调内核；经 `engine/` 适配 |
| `pobi_v2/engine/` | 任务执行管线、事件总线、ARQ worker、审批引擎、护栏、报告 | 被 router 调用 | 禁止跨模块循环依赖 |
| `pobi_v2/db/` | 模型定义、session、落库辅助 | ORM 模型 | 禁止在 model 写业务；持久化逻辑放 `persistence.py` |
| `pobi_v2/core/` | 配置/异常/安全/鉴权依赖/seed | `get_current_user` 等 | 安全密钥走环境变量，禁止硬编码 |
| `pobi_v2/llm/` | 统一 LLM 抽象层 | `complete/complete_json/chat`、`ModelSpec` 解析 | 当前为预留层，消费方未接入 |
| `pobi_agent/`（根） | AI 内核：CoreAgent / DeadEndAgent / 工具 / 子 Agent | 被 `engine/deadend_runner.agent_adapter` 驱动 | 视为外部内核，改动需追溯上游 pobi |
| `web/` | 前端 SPA（index.html + static/css + static/js），零构建 | `/app` 托管 | 禁止引入 Node 构建步骤 |

## 核心数据流

1. `POST /api/v1/tasks` 创建任务 → 护栏校验 scope → 状态 `queued` → 入队 ARQ。
2. ARQ Worker 拉起 `engine/executor.py` → 分流 `deadend_runner`（M8 主路径，驱动 `DeadEndAgent`）或 `probe_runner`（probe 快路径，绕过 avfs/多智能体）。
3. 运行期事件经 `pobi_agent.EventHooks` → `engine/event_bus.py` → 落库 `TaskEvent` + 会话级 token 累计；SSE 经 `routers/stream.py` 实时推送。
4. **侦察/利用产物旁路落库**：supervisor 调用 requester/shell/webapp_analyzer（侦察与利用共用同一 `RequesterAgent`，仅提示词不同）后，在 `agents/components/executor.py` 的 `_add_agent_output_to_context` 内调用 `_persist_recon_facts`，解析 agent 输出文本中的端点 / 技术栈，经 `ContextEngine.add_discovered_fact`（落 `recon_facts`）与 `ContextEngine.add_recon_endpoint`（落 `recon_endpoints`）旁路写入本地 SQLite（`~/.pobi_v2/tasks/<task_id>/recon/<task_id>.db`）。该通道在 agent 运行期随跑随写、异常仅记 warning 不阻断主循环，**任务取消不影响已落库数据**；`ContextEngine.recon_store` 未注入时全部 no-op。正式 `findings`/`task_events` 仍仅在 `_persist_outcome` 的 `completed` 路径写入（取消分支跳过）。
4. 高危工具调用 → `engine/approval.py` 创建 `ApprovalRequest`（checkpoint，失败关闭）→ 前端审批或 `auto_approve`。
5. 完成 → 状态 `completed`/`failed`/`cancelled`，`result` 写入；报告经 `routers/report.py` 导出。
6. SSE 断连 → `GET /api/v1/tasks/{id}/events`（`after_seq` 游标）回放，弥补断连即丢。

## 模块依赖关系

- `routers` → `engine` → `db` / `pobi_agent`（经 adapter）→ 外部依赖。
- `engine/event_bus` 对接 `pobi_agent.EventHooks`；`agent_adapter` 安装钩子（`main.py` lifespan）。
- 前端 `web/static/js/app.js` 经 `/api/v1/*` + SSE 与后端交互，同源 Cookie（须走 `/app`，禁止 `file://`）。
- 两层持久架构（README「记忆与缓存」）：Cache（`POBI_CACHE_HOME`，全局命名空间）与 Memory（`ROOT_DEADEND_PATH/agents/<agent_id>/<task_id>/memory`，`agent_id` 命名空间）；二者生命周期不同，不可混淆。AVFS 按 `session_id × workspace` 建表，memory 读写必须用同一 `memory_session_id`（历史因误用 `session_id`(task_id) 阻塞 131 次）。
