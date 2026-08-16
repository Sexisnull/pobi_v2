# Pobi v2 项目目标文档（供后续开发 Agent 阅读）

> 本文件是项目总纲。新增功能、修复缺陷、重构模块前，请先读本文件，确保改动方向与项目目标一致。
> 参考项目的架构参考：`docs/deadend-cli架构解析.md`、deadend-cli引擎内核参考 `docs/deadend_cli_architecture.html` / `docs/deadend_dev_guide.html`。

---

## 1. 项目是什么（目标愿景）

**Pobi v2 是一个前后端分离的 AI 渗透测试 Web 平台**，重构自 `pobi`（deadend-cli 演进分支）。

它把原本只能单机命令行运行的 AI 自主渗透引擎封装为企业级 Web 服务，让安全团队能够：

- 在 **Web 控制台** 上创建「授权测试目标」与「渗透任务」；
- 通过 **实时事件流** 观察 AI Agent 的思考、工具调用、置信度；
- 对 **高危工具调用进行人工审批（fail-closed 护栏）**，避免越权或危险操作；
- 以 **多租户** 方式隔离不同团队的数据与权限；
- 导出 **结构化报告（Markdown / JSON）** 用于交付与审计；
- 所有行为留存 **审计日志**，满足合规与可追溯要求。

**最终形态**：一个「可信可用的 Web 版 deadend-cli」——既保留原引擎的多智能体协作、Docker 沙箱验证、ADaPT 递归规划等核心能力，又叠加 Web 平台独有的多租户、持久化、实时流、审批护栏、报告导出等增量价值。

---

## 2. 当前状态（截至 2026-08）

### 2.1 已完成的里程碑（M1–M8，均已落地）

| 里程碑 | 内容 | 关键代码 |
|--------|------|----------|
| M1 | FastAPI 骨架 + SQLAlchemy 2.0 + Alembic（PostgreSQL）+ Pydantic Schema + 统一异常 | `main.py` / `db/` / `core/exceptions.py` |
| M2 | ARQ 任务队列 + SSE 实时推送 + 授权范围护栏（ScopePolicy） | `engine/queue.py` / `stream.py` / `guardrails.py` |
| M3 | 运行轨迹 / 发现 / 审计 / 产物落库，支持取消与续跑 | `engine/persistence.py` / `executor.py` / `cancel_state.py` |
| M4 | 多租户鉴权：User/Tenant + JWT + 资源租户隔离 | `routers/auth.py` / `core/security.py` / `core/deps.py` |
| M5 | 审批护栏（fail-closed）+ 结构化报告导出 | `engine/approval.py` / `routers/approval.py` / `routers/report.py` |
| M6 | 纯静态前端 SPA（vanilla JS，零构建），覆盖登录/目标/任务/审批/审计 | `web/` 由 `main.py` 挂载 `/app` |
| M7 | 轻量扫描工作流 `ScanWorkflow` 复刻 DeadEndAgent 三阶段（threat_model → 利用循环 + ValidationGate → report），复用 ScopePolicy / ValidationGate / ReporterAgent | `engine/scan_workflow.py` / `scan_tools.py` |
| M8 | 直接驱动原 `DeadEndAgent` 完整多智能体系统（6 个子 Agent + Docker 沙箱 + 多 LLM + ADaPT + ValidationGate + ReporterAgent） | `engine/deadend_runner.py` |

### 2.2 近期已演进（未在 README 详述的新能力）

- **任务执行模式 `agent_mode`**：`Task.agent_mode` 支持 `hacker`（默认，高危调用需审批）与 `yolo`（自动批准高危调用）。相关迁移：`alembic/versions/0006_task_mode.py`。
- **审批回调已修复**：`engine/approval.py` 中回调按独立 `tool_call_id` 创建 `ApprovalRequest` 并等待决策，避免同任务多次高危调用主键冲突。
- **启动自动 seed admin**：库内无用户时，`core/seed.py` 幂等创建 `admin@example.com` + 默认租户（凭证来自 `POBI_V2_ADMIN_*` 配置）。
- **`auto_approve` 配置**：`POBI_V2_AUTO_APPROVE=true` 时审批回调自动批准高危调用（用于授权靶场自动化；默认 fail-closed 拒绝）。
- **运行指令通道（M8+ 新增）**：`engine/instruction_channel.py` + `routers/instruction.py`。用户可经 `POST /api/v1/tasks/{id}/instructions` 向运行中任务追加指令，Worker 在 `run_exploitation` 协作式检查点 `drain_instructions` 消费并注入 Supervisor 上下文。与 `cancel_state` 同构（memory/redis 双后端）。
- **系统状态与任务对账（M8+ 新增）**：`routers/system.py`。`GET /api/v1/system/worker-status` 探测 ARQ Worker 在线情况与队列积压（基于 health-check 键）；`POST /api/v1/system/task-reconcile` 对账 PG 活跃任务与 ARQ 队列真实状态，收敛幽灵任务（取消标志/队列丢失/超时三类终止）。
- **任务实时态聚合（M8+ 新增）**：`GET /api/v1/tasks/{id}/live` 返回 `TaskLiveState`（当前阶段/智能体/执行计划 `PlanSummary`/待生效指令数/最近事件），供控制台中栏展示。
- **统一 LLM 入口（M8+ 收敂）**：`pobi_v2/llm/` 为平台唯一 LLM 解析与调用入口（`get_model_spec` 产出内核 `ModelSpec`，`complete/complete_json/chat` 供平台自包含调用复用）。凭证统一前缀 `POBI_V2_LLM_API_KEY`/`POBI_V2_LLM_API_BASE` 优先，缺失按 provider 回退裸供应商变量（兼容既有 `.env`）。所有执行路径（M8 主路径、M7 降级、默认 agent）均经此入口，不再各自拼写环境变量映射。

### 2.2.1 链路验证与稳定性修复（2026-08-15，端到端验证闭环）

> 本节记录一次端到端链路验证中发现并修复的问题，作为后续 agent 排障参考。

- **端到端链路验证（probe 快路径，新增能力）**：`POST /api/v1/system/probe` 在共享 Kali 沙箱对授权目标做轻量连通性探测（`curl` 访问 + 单次 LLM 结论），由新增 `engine/probe_runner.py` 的 `run_probe_agent` 直接驱动。特性与边界：
  - **probe 自身绕过 avfs / DeadEndAgent**：仅 probe 走轻量快路径，避免 dev 环境 avfs 未挂载导致多智能体初始化卡死；探测直接在共享 Kali 沙箱执行（`sandbox_manager.get_or_create_shared_kali()`）。注意这是**链路验证专用的窄路径**——正常渗透任务（`task.kind` 非 probe）仍由 `executor` 驱动 M8 `DeadEndAgent` 完整多智能体链路（规划+利用+ValidationGate+报告），二者互不替代。
  - **硬超时 90s** 由 `asyncio.wait_for(..., PROBE_HARD_TIMEOUT)` 在 `executor._run_probe_branch` 内保证（arq 0.28 不支持作业级 `job_timeout` 透传，故超时置于 executor 层）。
  - `executor._run_task_body` 按 `task.kind == "probe"` 分流到 probe 分支，与 M8 主路径 / M7 降级并列。
  - 结果异步返回：`/probe` 立即返回 `task_id`，结论经 `GET /tasks/{id}` 或 SSE 拉取；实测一次探测约 4 秒返回（HTTP 302，0.07s）。
  - 前端新增「健康检查」页：聚合 Worker / Kali / 模型三段实时状态（`/system/worker-status`、`/system/kali-status`、`/system/llm-status`），「发起健康探测」按钮触发 probe 并轮询结果，同时展示上一次探测结论。
- **Worker 卡死修复**：`docker-compose.override.yml` 的 worker command 原含 `--watch /app`，文件变动触发 SIGUSR1 重启后会卡在初始化、不再刷新 health-check 键（TTL 变负），表现为「假活离线」。已移除 `--watch`，改为改源码后手动 `docker compose restart worker` 生效。
- **取消检查死锁 bug 修复（关键）**：原 `executor.py` 在 Worker 协程内调用 `cancel_state.is_cancelled_sync`，其 Redis 后端用 `run_coroutine_threadsafe` + `future.result(timeout=2)`，在事件循环协程内调用导致死锁超时，进而**所有任务执行失败**。已改为异步 `await is_cancelled(task_id)`（导入由 `is_cancelled_sync` 改 `is_cancelled`），异常分支同步改为异步。
- **僵尸任务治理**：Worker 重启 / 卡死期间遗留的 `running` / `queued` 任务不会自动终态化。治理方式：经 `POST /api/v1/system/task-reconcile` 对账收敛，或运维直接将遗留任务置 `failed` 终态并备注来源，避免干扰任务列表查询。
- **进程入口收敛**：`docker-compose.yml` 统一服务命名（`pobi_v2-api-1` / `pobi_v2-worker-1` / `pobi_v2-web-1`）；前端静态由 `web`(nginx) 经 bind mount `./web` 实时托管于 80 端口，访问入口为 `http://<host>/`（非 8000 api 端口的镜像内旧静态）。

### 2.3 已知约束 / 待补能力（待办路线）

按优先级（非阻塞）：

| 编号 | 任务 | 优先级 | 说明 |
|------|------|--------|------|
| C1 | 引入 XBOW 等评测子集，跑通基准 + 输出报告 | P0 | 当前无量化 benchmark，无法客观评估能力 |
| C3 | ~~组件健康监控面板~~ | ✅已解决 | 已由前端「健康检查」页落地：聚合 Worker / Kali / 模型三段实时状态 + 一键端到端链路探测（probe 快路径），对齐 deadend-cli `showComponentStatus` |
| C4 | Plan Mode（规划预审） | P1 | 对齐 `/plan`，执行前人工确认攻击计划 |
| C5 | 白盒分析启用（`codebase_path`） | P1 | 依赖 Playwright / Embedder / RAG，当前默认关闭，缺失时降级黑盒 |
| C6 | 攻击链复用（Task 模板） | P2 | 对齐 workflow replay |
| C7 | 报告模板化 | P2 | 对齐 `/report` templating |

### 2.4 架构与规范性待治理项（2026-08-13 评审新增）

> 以下为本次架构评审识别的工程治理项，按优先级排列，非功能性阻塞，但须收敛：

| 编号 | 问题 | 优先级 | 说明 / 处置建议 |
|------|------|--------|------------------|
| A1 | **分层倒置**：内核反向依赖平台层 | P0 | `pobi_agent/pobi_agent.py` 反向 `import pobi_v2.engine.instruction_channel`，构成抽象循环，违背「engine 编排、内核不感知平台」边界。应经既有钩子/回调注入点透传指令，或把指令通道下沉为内核可注入的协议（如 `InstructionSink` 接口），消除内核对 pobi_v2 的硬依赖 |
| A2 | `python_scripts/` 失序 | P1 | 堆积 20+ 个 DVWA 一次性测试脚本（`dvwa_auth.py`/`dvwa_auth2.py`/`probe.py`/`test.py`/`minimal_test.py` 等），大量重复，违反「临时验证文件须清理」。应归档至 `scripts/dvwa/` 或删除 |
| A3 | `logs/` 被纳入版本控制 | P1 | `api.log`/`worker.log`/`worker_run.log` 应加入 `.gitignore`，从版本控制移除 |
| A4 | ~~`pobi_v2/llm/` 为孤儿模块~~ | ✅已解决 | 已改造为平台唯一 LLM 解析与调用入口（单一 `get_model_spec` 产出内核 `ModelSpec`，凭证 `POBI_V2_LLM_*` 优先 + 裸供应商变量兼容）。`deadend_runner`/`scan_workflow`/`agent_adapter`/`executor` 三条路径全部经此入口，消除主/降级路径分叉与 `POBI_V2_LLM_API_KEY` 不生效问题；其 `complete/complete_json/chat` 封装供平台自包含调用复用。 |
| A5 | CORS 配置不安全 | P2 | `main.py` 同时设 `allow_origins=["*"]` 与 `allow_credentials=True`，被浏览器规范禁止；生产须收敛为显式来源 |
| A6 | `main.py:web_app` 分支矛盾 | P2 | `if not index.exists(): return FileResponse(index) if index.exists() else ...` 自相矛盾，须修正为不存在时回退 README |
| A7 | `routers/system.py` 脆弱写法 | P3 | `len(active) if "active" in dir() else 0` 风格不佳，宜在 except 中初始化 `active = []` |

> 注：旧文档 `PROJECT_STATUS.md` 记录的 F1–F5 缺陷（record_audit 签名、system_prompt、llm_model、审批回调、审计租户）**均已修复**，请勿再作为待办。

### 2.5 扫描内核后续优化方向（2026-08-13 评审新增）

> 以下为扫描内核（pobi_agent）后续迭代的核心优化方向，供后续开发 Agent 排期参考。
> 这些问题均属于「能力增强 / 架构演进」范畴，非当前阻塞项，但决定内核从「能跑」到「好用」的跃迁。

| 编号 | 问题 | 类别 | 说明 / 优化方向 |
|------|------|------|------------------|
| S1 | **侦查阶段内容无格式化、无向量化** | 数据治理 | 当前侦查产出（端点/技术栈/认证面等）以纯文本/Markdown 落入上下文，未做结构化抽取，也未向量化入库。应定义统一侦查产物 schema（如 `ReconFinding` 结构化对象），并对可检索内容做 embedding 入库，支撑后续 RAG 检索而非全文堆上下文 |
| S2 | **侦查产物全部加载至上下文** | 上下文管理 | 当前 `ContextEngine.get_unified_context` 将侦查结果整段塞入上下文（见 `run_exploitation` 传 `previous_context`），随目标规模膨胀导致上下文爆炸、噪声淹没、成本攀升。应改为「按需检索 + 摘要 + 向量召回」，仅在利用阶段拉取与当前子目标相关的侦查片段 |
| S3 | **侦查阶段调用工具太少，连指纹识别都没有** | 能力缺口 | 当前侦查阶段子 Agent 调度偏重 RAG 检索与基础请求，缺少主动指纹识别能力（技术栈识别、banner/版本探测、中间件/框架指纹、WAF/CDN 识别等）。应补充指纹识别类工具（如 `fingerprint_target` / 集成 Nuclei-template 探测 / WhatWeb 风格识别），把「识别」从「LLM 猜」下沉为确定性的工具产出 |
| S4 | **利用阶段调用工具太少，没有常见漏洞利用工具** | 能力缺口 | 当前利用阶段主要靠 LLM + 子 Agent 自由编排 HTTP/Shell/Python，缺少常见漏洞利用工具链（如 Sqlmap、Xray、Nuclei 漏洞模板、SSRF/XXE 专项 payload 库等）。应把成熟利用工具封装为可审批、可复用的 `Tool`，让 LLM 决策「调哪个工具」而非「手写每一步 exploit」 |
| S5 | **Supervisor prompt 过于针对化（假设目标必有 flag）** | 提示词治理 | 通用 `supervisor.instructions.jinja2` 含「每端点最多 2 次调用、无 flag 即转向」等利用期措辞，隐含「目标是靶场、必有 flag」假设，对真实业务系统不适用（无 flag、需按风险面推进）。应按阶段拆分：侦查阶段用「信息充分即停」软约束，利用阶段按漏洞确认/风险证据而非 flag 推进；避免把靶场假设硬编码进通用 prompt |
| S6 | **LLM 应专注决策，固定性动作交给可调用的工具** | 架构原则 | 当前 LLM 既做决策又承担大量可确定化的执行细节（手写请求、手工拼 payload、重复推断）。应明确分工边界：**LLM 负责任务分解、工具选择、结果研判；确定性/高频/易错动作封装为可调工具**（指纹识别、漏洞扫描、exploit 模板、标准化请求构造器等），降低幻觉与 token 消耗，提升可复现性 |

> 核心原则（S6 延伸）：把内核从「LLM 全包」演进为「LLM 调度 + 工具执行」的确定性协作范式——LLM 做规划与判断，工具做动作与产出，上下文只承载决策所需的最小相关信息（呼应 S1/S2）。

---

## 3. 架构总览

### 3.1 分层架构

```
┌─────────────────────────────────────────────────────────────┐
│  前端 SPA (web/, vanilla JS)  ── /app, /static, /web/*       │
│  · 登录/注册 · 目标管理 · 任务管理 · SSE 实时流 · 审批 · 报告 │
└───────────────────────────┬─────────────────────────────────┘
                            │ HTTP / SSE / Bearer JWT
┌───────────────────────────▼─────────────────────────────────┐
│  FastAPI 应用 (pobi_v2/main.py)                              │
│  routers: auth / targets / tasks / stream / persistence /    │
│           approval / report                                  │
│  core: config / exceptions / security(JWT+bcrypt) / deps /   │
│        seed                                                  │
└───────┬───────────────────────────┬─────────────────────────┘
        │                           │
┌───────▼──────────┐    ┌───────────▼──────────────────────────┐
│  DB 层           │    │  Engine 层（任务执行与 Agent 编排）    │
│  SQLAlchemy 2.0  │    │  executor → deadend_runner /         │
│  + Alembic       │    │    scan_workflow → agent_adapter     │
│  models:         │    │  queue(ARQ) / worker /               │
│  Tenant/User/    │    │  event_bus / guardrails /            │
│  Target/Task/    │    │  approval / report / cancel_state    │
│  ApprovalRequest/│    │                                       │
│  Finding/        │    └───────────┬──────────────────────────┘
│  AuditEvent/     │                │ 复用 pobi_agent 内核
│  TaskEvent/      │    ┌───────────▼──────────────────────────┐
│  Artifact        │    │  pobi_agent（源自 deadend-cli）        │
└──────────────────┘    │  DeadEndAgent（M8）/ CoreAgent(M2)    │
        │               │  ScopePolicy / ValidationGate /      │
┌───────▼──────────┐    │  ReporterAgent / EventHooks /        │
│  PostgreSQL      │    │  6 子 Agent / Docker 沙箱 (Kali)     │
│  Redis (ARQ+事件) │    └───────────────────────────────────────┘
└──────────────────┘
```

### 3.2 关键数据流：一次渗透任务

1. `POST /api/v1/targets` 创建授权目标（`in_scope` / `out_of_scope` JSONB）。
2. `POST /api/v1/tasks` 创建任务 → `guardrails` 校验授权范围 → 状态 `queued` → 入 ARQ 队列。
3. `worker` 取出任务 → `executor` 驱动 `deadend_runner`（M8 完整引擎）或 `scan_workflow`（M7 轻量）→ `agent_adapter` 挂载事件钩子与审批回调。
4. Agent 运行事件经 `event_bus`（memory/Redis）实时写入 DB + SSE 推送（`GET /api/v1/tasks/{id}/stream`）。
5. 高危工具调用触发 `approval` → 前端审批 → 回调放行/拒绝（fail-closed）。
6. 完成 → 轨迹 / findings / artifacts 落库 → `GET /api/v1/tasks/{id}/report[/markdown|/json]` 导出。

### 3.3 任务分发与 Agent 编排链路（端到端）

本节能帮后续 agent 弄清「engine 层到底做了什么、kernel 又做了什么」。链路如下：

```
前端 POST /tasks
   │  写 PG(Task: queued) + enqueue_task(task_id)
   ▼
FastAPI routers/tasks  ──► [Redis 队列] push "run_task"
   │ 立即返回 202                 │
   ▼                              │ Worker 阻塞式 pop
                            [engine/executor.run_task]
                             · 加载 Task/Target，护栏 assert_in_scope
                             · 状态 running
                             · 建 approval_cb（fail-closed，yolo 免审批）
                             · 委托 deadend_runner（沙箱不可用回退 ScanWorkflow）
                                    │
                                    ▼
                            [pobi_agent.DeadEndAgent]
                             threat_model → run_exploitation → report
                             （Phase 1 与 Phase 2 共用同一套 supervisor+子 Agent
                              引擎，仅 goal prompt 不同；Docker 沙箱验证；
                              ADaPT 递归规划；ValidationGate 验证；ReporterAgent 报告）
                                    │ 事件经 PobiV2EventHooks 发往 event_bus
                            ┌───────┴────────┐
                            ▼                ▼
                      [Redis pub/sub]    [PG TaskEvent 落库]
                            │
                            ▼
                      前端 SSE 实时显示思考/工具调用
```

**（1）engine 如何「分发」任务**
分发是轻量的、同步的、毫秒级：`routers/tasks` 创建任务写 PG（状态 `queued`）后，仅把 `task_id` 字符串通过 `queue.enqueue_task` 丢进 Redis 队列（`redis.enqueue_job("run_task", task_id)`），接口立即返回。真正的「派活」由 Worker 从队列领走，engine 自己不直连 Agent。

**（2）engine 如何「编排」Agent（边界：engine 不写渗透逻辑）**
`executor.run_task` 是编排核心，职责是「准备环境 → 适配输入 → 委托内核 → 回收产出」：
- 加载 `Task/Target`，`assert_in_scope` 授权闸门（失败直接 `failed`）；
- 状态推进 `queued → running`；
- `make_approval_callback` 把 Web 平台的多租户人工审批注入内核高危调用（yolo 模式免审批）；
- 委托 `deadend_runner.run_deadend_agent`（适配层，不重写内核）：把 pobi_v2 的 `Target/Task` 翻译成 pobi_agent 输入——写 `scope.yaml`（复用 ScopePolicy）、写 `validation.yaml`（复用 ValidationGate）、解析 `ModelSpec`（多 LLM）、按 Docker 可用性决定开不开 `shell/python_interpreter`；
- 主路径 `DeadEndAgent` 沙箱不可用时，自动降级到不依赖沙箱的 `ScanWorkflow`；
- **仅 `task.kind == "probe"` 走轻量快路径**：`_run_probe_branch` 直接调用 `probe_runner.run_probe_agent`（共享 Kali 沙箱内 `curl` 连通性探测 + 单次 LLM 结论），probe 自身绕过 avfs / DeadEndAgent 多智能体链路，由 `asyncio.wait_for(..., PROBE_HARD_TIMEOUT=90s)` 兜底；正常渗透任务仍走 M8 `DeadEndAgent`（见上一条）。结果同样经 `_persist_outcome` 落 PG，结论写入 `Task.result`；
- 内核跑完后 `_persist_outcome` 把结果/findings/轨迹落 PG。
真正的多智能体协作在 `DeadEndAgent` 内部：Phase 1 侦查（`threat_model`）与 Phase 2 利用（`run_exploitation`）**均经 `execute_supervisor` 驱动同一套 `SupervisorAgent` + 6 子 Agent 引擎**，仅传入的 `goal prompt` 不同（侦查收集端点/技术栈/认证/攻击面，利用做 ADaPT 递归求解）；其余 Docker 沙箱验证、ADaPT、ValidationGate、ReporterAgent 亦归 pobi_agent 内核，engine 不管。

**（3）Worker 如何工作**
- 启动 `uv run arq ...WorkerSettings` 后，进程连 Redis 阻塞监听 `run_task` 队列（`functions=[run_task]`、`job_timeout=6h`、`max_tries=2`）；
- 竞争消费：任务被一个 Worker `pop` 走后即从队列移除，不会被重复执行；多开 Worker = 提高并发（本地开发开 1 个即可）；
- 执行期间内核每步事件经 `event_bus` 实时流出：Redis 后端由 FastAPI 订阅推 SSE，Memory 后端由 Worker 内 `persist_event_worker` 协程落 PG；
- 取消：前端写 Redis 取消标志，Worker 循环读到标记 `cancelled`；异常：`max_tries` 自动重投重试，多次失败标记 `failed`。

> 一句话：FastAPI 收任务入队即返回；Worker 领 `task_id` 调 `run_task`；`run_task` 做分发+适配+护栏+回收，把活委托给 `deadend_runner` 适配层；适配层驱动原 `DeadEndAgent` 三阶段渗透；事件经 event_bus 实时推前端、结束落 PG。**engine 层不写渗透代码，只做编排与回收。**

### 3.4 目录结构（精简）

```
pobi_v2/
├── main.py                 # FastAPI 入口，挂载前端与路由
├── core/                   # config / exceptions / security / deps / seed
├── db/                     # session / models / persistence
├── schemas/                # target / task（含 PlanStep/TaskLiveState/TaskInstructionIn）/ auth / approval / persistence
├── routers/                # auth / targets / tasks / stream / persistence / approval / report
│                           # + instruction（运行指令）/ system（Worker 状态 + 任务对账）
├── llm/                    # 统一 LLM 解析与调用入口（get_model_spec + complete/complete_json/chat，复用内核 ModelSpec）
└── engine/                 # executor（含 probe 分流）/ deadend_runner / probe_runner（链路验证快路径）
                             # scan_workflow / scan_tools / agent_adapter / event_bus
                             # guardrails / approval / queue / worker / cancel_state / report
                             # instruction_channel（运行指令通道，与 cancel_state 同构）
pobi_agent/                 # 内嵌 AI 引擎（源自 deadend-cli，位于仓库根目录，非 pobi_v2/ 子包）
web/                        # M6 前端 SPA（index.html + static/）
alembic/                    # 数据库迁移（0001–0007）
docker-compose.yml          # postgres + redis
Dockerfile.prod             # 多阶段构建（AGENTS.md 规范）
```

---

## 4. 技术约束与开发规范（必读）

遵循根目录 `AGENTS.md`。要点：

- **安全红线**：高危操作默认 fail-closed；`ScopePolicy` 授权闸门不可绕过；审批回调缺失时一律拒绝。
- **极简设计**：少即是多，函数职责单一、自解释；优先标准库，显式捕获异常。
- **多租户隔离**：所有数据接口必须按 `tenant_id` 隔离，禁止跨租户读取。。
- **Nginx**：更新 `nginx.conf` 必须开启 Gzip，并对 SSE 路径关闭代理缓冲。
- **文档语言**：生成内容与 Artifacts 用中文；提交信息用客观工程化表述，禁止主观营销词汇。
- **破坏性操作**（删容器 / `git reset --hard` / `git clean -fd` 等）须先说明影响并获批。
- **许可证**：AGPL-3.0，网络服务须提供源码获取途径（见 `LICENSE` / `NOTICE`）。

---

## 5. 快速上手（开发）

```bash
uv sync                                   # 依赖（uv workspace）
docker compose up -d                      # postgres + redis
uv run alembic upgrade head               # 迁移
uv run uvicorn pobi_v2.main:app --reload --port 8000   # 后端
uv run arq pobi_v2.engine.worker.WorkerSettings         # 另开终端：Worker
# 前端：http://localhost:8000/app
# API 文档：http://localhost:8000/docs
```

首次启动若无用户，自动 seed `admin@example.com` / `admin123456`（可用 `POBI_V2_ADMIN_*` 覆盖）。

---

*本文件随代码演进维护。删除或重大调整里程碑时，请同步更新第 2 节与构建路线。*
