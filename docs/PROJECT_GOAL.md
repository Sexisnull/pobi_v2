# Pobi v2 项目目标与平台状态文档（供后续开发 Agent 阅读）

> 本文件是项目总纲。新增功能、修复缺陷、重构模块前，请先读本文件，确保改动方向与项目目标一致。
> 后续 Agent 直接读 `README.md` 与本文件即可掌握全貌，无需再翻阅 `docs/PLATFORM_STATUS.md`（其历史内容已并入本文件第 2、4、5 节）。
> 内核参考：`docs/deadend-cli架构解析.md`、引擎内核参考 `docs/deadend_cli_architecture.html` / `docs/deadend_dev_guide.html`。

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

## 2. 当前平台状态（截至 2026-08）

### 2.1 项目架构概览

PoBi v2 是一个**前后端分离的 AI 智能体驱动授权渗透测试平台**，面向安全靶场/授权站点的自动化侦察、漏洞利用与报告生成。

#### 2.1.1 分层结构

```
浏览器/客户端
   └─ FastAPI 网关（pobi_v2/main.py）
        ├─ routers/         # REST + SSE 接口层（11 个路由）
        ├─ engine/          # 任务编排：executor / event_bus / agent_adapter / memory / queue / worker
        ├─ db/              # SQLAlchemy 2.0 异步模型与持久化
        ├─ llm/             # 统一 LLM 解析与调用入口
        └─ services/        # 跨域服务（邮件、定价等）
   └─ pobi_agent（内核）    # 纯 Python 智能体内核（监督者/规划/工具调用）
        ├─ core_agent/      # 主智能体循环
        ├─ tools/           # 网络请求 pw_send_payload、浏览器、文件、沙箱执行等
        └─ agents/          # RequesterAgent、ExploitAgent 等专业子智能体
   外部依赖：
        ├─ PostgreSQL       # 主库（多租户隔离）
        ├─ Redis + ARQ      # 任务队列与后台 worker
        └─ Kali 沙箱容器     # 隔离命令执行环境（docker 运行）
```

#### 2.1.2 里程碑进度

| 里程碑 | 内容 | 状态 |
|---|---|---|
| M1 | 项目初始化（FastAPI + 多模块结构 + 配置） | ✅ 完成 |
| M2 | 数据模型（User/Target/Task/Finding/Audit 等） | ✅ 完成 |
| M3 | 认证与多租户（JWT、ApiToken、租户隔离） | ✅ 完成 |
| M4 | 任务执行引擎 + ARQ worker + 沙箱 | ✅ 完成 |
| M5 | 用户注册登录 / 目标管理 / 任务管理 API | ✅ 完成 |
| M6 | 持续会话 + 记忆（memory workspace） | ✅ 完成 |
| M7 | 安全护栏（自动批准 / 人工审批 / 危险动作拦截） | ✅ 完成 |
| M8 | 报告导出（HTML/Markdown/JSON）+ 使用量统计 | ✅ 完成 |
| M8+ | 运行指令通道 / 系统状态对账 / 统一 LLM 抽象层 / Token 用量统计 / 端到端链路验证（probe 快路径） | ✅ 完成 |

#### 2.1.3 当前核心能力

- 多租户 SaaS 隔离、JWT 鉴权、API Token（PAT）。
- 基于内核 `pobi_agent` 的任务编排，监督者智能体驱动 RequesterAgent / ExploitAgent。
- 任务实时 SSE 流、事件落库（TaskEvent）、发现/产物落库（Finding/Artifact）。
- 人工审批网关、危险命令检查点、报告导出。
- **并发消费**：ARQ 原生按 `max_jobs` 并发（默认 2，见 `pobi_v2/core/config.py: worker_max_jobs`），多开 Worker 副本（`docker-compose.yml` 的 `deploy.replicas`）即可横向提升并发；单 worker 时多余任务在队列排队，由 ARQ 原生调度，不会重复消费。
- **LLM 限流**：进程级 `asyncio.Semaphore`（`llm_max_concurrency`，默认 8）统一限流 LLM 并发调用，规避上游 429（发生在限流层而非结果串台）。

### 2.2 近期已演进（未在 README 详述的新能力）

- **任务执行模式 `agent_mode`**：`Task.agent_mode` 支持 `hacker`（默认，高危调用需审批）与 `yolo`（自动批准高危调用）。相关迁移：`alembic/versions/0006_task_mode.py`。
- **审批回调已修复**：`engine/approval.py` 中回调按独立 `tool_call_id` 创建 `ApprovalRequest` 并等待决策，避免同任务多次高危调用主键冲突。
- **启动自动 seed admin**：库内无用户时，`core/seed.py` 幂等创建 `admin@example.com` + 默认租户（凭证来自 `POBI_V2_ADMIN_*` 配置）。
- **`auto_approve` 配置**：`POBI_V2_AUTO_APPROVE=true` 时审批回调自动批准高危调用（用于授权靶场自动化；默认 fail-closed 拒绝）。
- **运行指令通道（M8+ 新增）**：`engine/instruction_channel.py` + `routers/instruction.py`。用户可经 `POST /api/v1/tasks/{id}/instructions` 向运行中任务追加指令，Worker 在 `run_exploitation` 协作式检查点 `drain_instructions` 消费并注入 Supervisor 上下文。与 `cancel_state` 同构（memory/redis 双后端）。
- **系统状态与任务对账（M8+ 新增）**：`routers/system.py`。`GET /api/v1/system/worker-status` 探测 ARQ Worker 在线情况与队列积压（基于 health-check 键）；`POST /api/v1/system/task-reconcile` 对账 PG 活跃任务与 ARQ 队列真实状态，收敛幽灵任务（取消标志/队列丢失/超时三类终止）。
- **任务实时态聚合（M8+ 新增）**：`GET /api/v1/tasks/{id}/live` 返回 `TaskLiveState`（当前阶段/智能体/执行计划 `PlanSummary`/待生效指令数/最近事件），供控制台中栏展示。
- **统一 LLM 入口（M8+ 收敛）**：`pobi_v2/llm/` 为平台唯一 LLM 解析与调用入口（`get_model_spec` 产出内核 `ModelSpec`，`complete/complete_json/chat` 供平台自包含调用复用）。凭证统一前缀 `POBI_V2_LLM_API_KEY`/`POBI_V2_LLM_API_BASE` 优先，缺失按 provider 回退裸供应商变量（兼容既有 `.env`）。所有执行路径（M8 主路径、M7 降级、默认 agent）均经此入口。

#### 2.2.1 链路验证与稳定性修复（2026-08-15，端到端验证闭环）

> 本节记录一次端到端链路验证中发现并修复的问题，作为后续 agent 排障参考。

- **端到端链路验证（probe 快路径，新增能力）**：`POST /api/v1/system/probe` 在共享 Kali 沙箱对授权目标做轻量连通性探测（`curl` 访问 + 单次 LLM 结论），由新增 `engine/probe_runner.py` 的 `run_probe_agent` 直接驱动。特性与边界：
  - **probe 自身绕过 avfs / DeadEndAgent**：仅 probe 走轻量快路径，避免 dev 环境 avfs 未挂载导致多智能体初始化卡死；探测直接在共享 Kali 沙箱执行（`sandbox_manager.get_or_create_shared_kali()`）。注意这是**链路验证专用的窄路径**——正常渗透任务（`task.kind` 非 probe）仍由 `executor` 驱动 M8 `DeadEndAgent` 完整多智能体链路（规划+利用+ValidationGate+报告），二者互不替代。
  - **硬超时 90s** 由 `asyncio.wait_for(..., PROBE_HARD_TIMEOUT)` 在 `executor._run_probe_branch` 内保证（arq 0.28 不支持作业级 `job_timeout` 透传，故超时置于 executor 层）。
  - `executor._run_task_body` 按 `task.kind == "probe"` 分流到 probe 分支，与 M8 主路径 / M7 降级并列。
  - 结果异步返回：`/probe` 立即返回 `task_id`，结论经 `GET /tasks/{id}` 或 SSE 拉取；实测一次探测约 4 秒返回（HTTP 302，0.07s）。
  - 前端新增「健康检查」页：聚合 Worker / Kali / 模型三段实时状态（`/system/worker-status`、`/system/kali-status`、`/system/llm-status`），「发起健康探测」按钮触发 probe 并轮询结果，同时展示上一次探测结论。
- **Worker 卡死修复**：`docker-compose.override.yml` 的 worker command 原含 `--watch /app`，文件变动触发 SIGUSR1 重启后会卡在初始化、不再刷新 health-check 键（TTL 变负），表现为「假活离线」。已移除 `--watch`，改为改源码后手动 `docker compose restart worker` 生效。
- **取消检查死锁 bug 修复（关键）**：原 `executor.py` 在 Worker 协程内调用 `cancel_state.is_cancelled_sync`，其 Redis 后端用 `run_coroutine_threadsafe` + `future.result(timeout=2)`，在事件循环协程内调用导致死锁超时，进而**所有任务执行失败**。已改为异步 `await is_cancelled(task_id)`（导入由 `is_cancelled_sync` 改 `is_cancelled`），异常分支同步改为异步。
- **幽灵任务根因修复（风险1/2/3，2026-08）**：
  - **风险1（幂等字符串匹配）**：`queue.py` 原对 ARQ 入队异常做字符串匹配，实为死代码（ARQ 重复入队返回 `None` 不抛异常）；改为判 `job is None` 返回。
  - **风险2（兜底吞异常）**：`executor.py` 补 `logger.exception` 兜底落库，避免静默吞掉异常。
  - **风险3（取消后自动重试）**：`worker.py` 设 `retry_jobs=False`、`max_tries=1`，从根本消除 CancelledError 被重投导致的幽灵任务；`executor.py` 用 `is_cancelled(tid)` 区分用户取消（`cancelled`）vs 超时/重启（`failed`）。
- **僵尸任务治理**：Worker 重启 / 卡死期间遗留的 `running` / `queued` 任务不会自动终态化。治理方式：经 `POST /api/v1/system/task-reconcile` 对账收敛，或运维直接将遗留任务置 `failed` 终态并备注来源，避免干扰任务列表查询。
- **进程入口收敛**：`docker-compose.yml` 统一服务命名（`pobi_v2-api-1` / `pobi_v2-worker-1` / `pobi_v2-web-1`）；前端静态由 `web`(nginx) 经 bind mount `./web` 实时托管于 80 端口，访问入口为 `http://<host>/`（非 8000 api 端口的镜像内旧静态）。

#### 2.2.2 内核目录契约统一（2026-08，路径布局改造）

> 历史遗留目录（根目录 `validation.*.yaml`、`cache/scope.*.yaml`、`agents/<agent_id>/<task_id>`）已不再写入，新任务统一走下述结构。旧目录未清理，新任务只读新树。

任务运行期产出的中间产物、配置、记忆、RAG 索引统一按 **任务** 维度（单级）组织，根路径为 `POBI_HOME`（容器内默认 `/root/.pobi_v2`，与宿主 `~/.pobi_v2` 挂载对齐以保证跨重启保留）：

> ⚠️ **契约变更（2026-08）**：早期双级 `targets/<target_slug>/<task_id>/` 结构已废弃。内核不再感知授权目标 slug，统一以 `task_id`（== `session_id`）为目录主键单级归口到 `tasks/<task_id>/`。下列目录树为**当前唯一生效契约**，后续读写逻辑必须对齐此单级结构。

```
~/.pobi_v2/
└── tasks/
    └── <task_id>/                    # session_id == task_id 恒等式实现隔离
        ├── scope.<task_id>.yaml          # 授权范围（内核读取，网络出口硬闸门）
        ├── validation.<task_id>.yaml     # ValidationGate 配置
        ├── agent/                         # 子 Agent 记忆工作区
        │   └── <agent_id>/<session_id>/
        │       ├── workspace
        │       ├── memory
        │       ├── run_context
        │       └── auth_context
        ├── rag/                           # RAG 索引存储（SqliteRagConnector，运行期构建）
        ├── logs/
        │   ├── python_interpreter.jsonl
        │   └── <session_key>/requester.jsonl
        └── metrics/
            └── metrics.json
```

- `TASKS_ROOT = ROOT_DEADEND_PATH / "tasks"`（`pobi_agent/constants.py`）。所有任务级产物统一单级归口
  `tasks/<task_id>/`（task_id == session_id），内核仅持有 task_id，不感知授权目标 slug。
- `pw_requester._scope_path()` 改为优先 `storage_context.get_task_root() / f"scope.{session_id}.yaml"`（仅依赖 session_id）；未注入 task_root 时返回 `None`（走 ScopePolicy 默认禁用安全策略 fail-closed），**不再回退 slug 旧路径**。
- `deadend_runner.run_deadend_agent` 计算 `task_root = TASKS_ROOT / str(task_id)`，调用 `set_task_root(task_root)` 注入内核上下文，并将 `agents_storage_root=task_root/"agent"`、`storage_root=task_root/"rag"` 注入内核；任务结束 `clear_task_root()`。
- 内核散落点统一归口：`python_interpreter.jsonl`→`tasks/<task_id>/logs/`、`requester.jsonl`→`tasks/<task_id>/logs/<session_key>/`、`metrics.json`→`tasks/<task_id>/metrics/`、`run_context`/`auth_context`/`webpages`→`tasks/<task_id>/agent/<agent_id>/<session_id>/`。均优先 `get_task_root()`，回退旧路径兼容。
- 理解该机制是排查「缓存未落盘 / 子 Agent 读不到前序结论」类问题的基础，命名空间与生命周期以 `task_id` 隔离。

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

### 2.6 历史任务失败根因小结（代码级结论，仅作排障参考）

既有分析报告（前 3 份已删除，结论融入本文与 `docs/PLATFORM_STATUS.md`）：`AUTHENTICATOR_DVWA_FAILURE_ANALYSIS.md`（已删）、`SLOW_TASK_ANALYSIS.md`（已删）、`TASK_F3D1D6F6_HANG_ANALYSIS.md`（已删）、`TASK_MANUAL_CANCEL_ISSUES.md`。共性根因：

1. **认证阶段卡死（DVWA 场景）**：`browser_run_steps` 的 `BrowserStep` 不支持抓取 DVWA 的 CSRF `user_token`；提示词误选 Basic 认证；验证层假阳性 `valid=true`，导致登录死循环（实测 33 分钟空转，从未进入漏洞测试）。
2. **内存命名空间不一致（F3D1D6F6）**：`executor._persist_agent_summary` 写 memory 用 `task_id`，而内核挂载用 `agent_id`，命名空间不匹配，异常被静默吞掉，运行摘要/产物无法落库。
3. **沙箱命令无超时挂死（F3D1D6F6）**：`execute_command` 的 streaming 路径缺少命令超时，卡死任务永久悬挂。
4. **协作式取消不可靠（手动取消问题）**：任务卡死时检查点无法到达，取消信号收不到；产生「幽灵任务」——worker 已停止但 DB 仍显示 `running`，需 `POST /api/v1/system/task-reconcile` 强制对账。

> 上述 2/3/4 已在后续演进（认证真实校验 + 连续失败熔断、AVFS 命名空间统一、取消可靠性修复、幽灵任务根因修复）中收敛。任务失败为**已知工程缺陷的历史阶段**，非当前架构问题；产物缺失是落库时机被绑定到成功分支所致，改进方向见 `docs/THREAT_MODEL_STORAGE_DESIGN.md`（按 Target 维度持久化威胁建模信息）。

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
   ▼                              │ Worker 阻塞式 pop（max_jobs 并发）
                            [engine/executor.run_task]
                             · 加载 Task/Target，护栏 assert_in_scope
                             · 状态 running
                             · 建 approval_cb（fail-closed，yolo 免审批）
                             · LLM 调用经进程级 Semaphore 限流
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
- 委托 `deadend_runner.run_deadend_agent`（适配层，不重写内核）：把 pobi_v2 的 `Target/Task` 翻译成 pobi_agent 输入——写 `scope.yaml`（复用 ScopePolicy，落 `tasks/<task_id>/`）、写 `validation.yaml`（复用 ValidationGate，同级，验证策略按任务级配置 `_write_validation_config`）、解析 `ModelSpec`（多 LLM）、按 Docker 可用性决定开不开 `shell/python_interpreter`；
- 主路径 `DeadEndAgent` 沙箱不可用时，自动降级到不依赖沙箱的 `ScanWorkflow`；
- **仅 `task.kind == "probe"` 走轻量快路径**：`_run_probe_branch` 直接调用 `probe_runner.run_probe_agent`（共享 Kali 沙箱内 `curl` 连通性探测 + 单次 LLM 结论），probe 自身绕过 avfs / DeadEndAgent 多智能体链路，由 `asyncio.wait_for(..., PROBE_HARD_TIMEOUT=90s)` 兜底；正常渗透任务仍走 M8 `DeadEndAgent`（见上一条）。结果同样经 `_persist_outcome` 落 PG，结论写入 `Task.result`；
- 内核跑完后 `_persist_outcome` 把结果/findings/轨迹落 PG。
真正的多智能体协作在 `DeadEndAgent` 内部：Phase 1 侦查（`threat_model`）与 Phase 2 利用（`run_exploitation`）**均经 `execute_supervisor` 驱动同一套 `SupervisorAgent` + 6 子 Agent 引擎**，仅传入的 `goal prompt` 不同（侦查收集端点/技术栈/认证/攻击面，利用做 ADaPT 递归求解）；其余 Docker 沙箱验证、ADaPT、ValidationGate、ReporterAgent 亦归 pobi_agent 内核，engine 不管。

**（3）Worker 如何工作**
- 启动 `uv run arq ...WorkerSettings` 后，进程连 Redis 阻塞监听 `run_task` 队列（`functions=[run_task]`、`job_timeout=6h`、`retry_jobs=False`、`max_tries=1`、`max_jobs=settings.worker_max_jobs`）；
- 竞争消费：任务被一个 Worker `pop` 走后即从队列移除，不会被重复执行；多开 Worker 副本（`docker-compose.yml` 的 `deploy.replicas`）即横向提高并发，单 worker 内由 `max_jobs` 控制并行度，多余任务在队列排队由 ARQ 原生调度；
- 执行期间内核每步事件经 `event_bus` 实时流出：Redis 后端由 FastAPI 订阅推 SSE，Memory 后端由 Worker 内 `persist_event_worker` 协程落 PG；
- 取消：前端写 Redis 取消标志，Worker 循环读到标记 `cancelled`；异常：`retry_jobs=False` 已关闭自动重投（消除幽灵任务），取消与超时区分处理（用户取消=`cancelled`，重启/超时=`failed`）。

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
docker-compose.yml          # postgres + redis + api + worker(可副本) + web(nginx)
Dockerfile.prod             # 多阶段构建（AGENTS.md 规范）
```

---

## 4. 平台接口能力清单（全量）

下列接口基于 `pobi_v2/routers/` 源码逐项核查。鉴权列：`JWT` = 登录会话 Cookie/Bearer；`PAT` = API Token；`Admin` = 管理员权限；`公开` = 无需鉴权。所有受保护接口均按租户隔离。

### 4.1 认证 `auth.py`

| 方法 | 路径 | 鉴权 | 多租户 | 用途 |
|---|---|---|---|---|
| POST | `/api/v1/auth/login` | 公开 | 跨租户 | 邮箱+密码登录，签发 JWT |
| GET | `/api/v1/auth/me` | JWT | 是 | 当前用户信息 |
| POST | `/api/v1/auth/register` | 公开 | 跨租户 | 注册用户（归属租户 slug），返回 JWT |
| POST | `/api/v1/auth/tenants` | 公开 | 跨租户 | 创建租户 |

### 4.2 目标管理 `targets.py`

| 方法 | 路径 | 鉴权 | 多租户 | 用途 |
|---|---|---|---|---|
| GET | `/api/v1/targets/` | JWT | 是 | 列出当前租户目标 |
| POST | `/api/v1/targets/` | JWT | 是 | 创建目标（URL/范围） |
| GET | `/api/v1/targets/{target_id}` | JWT | 是 | 目标详情 |
| PUT | `/api/v1/targets/{target_id}` | JWT | 是 | 更新目标 |
| DELETE | `/api/v1/targets/{target_id}` | JWT | 是 | 删除目标 |

### 4.3 任务管理 `tasks.py`

| 方法 | 路径 | 鉴权 | 多租户 | 用途 |
|---|---|---|---|---|
| GET | `/api/v1/tasks/` | JWT | 是 | 任务列表（含 token 三列） |
| POST | `/api/v1/tasks/` | JWT | 是 | 创建任务（护栏校验 → 状态 `queued` → 入队 ARQ） |
| GET | `/api/v1/tasks/{task_id}` | JWT | 是 | 任务详情（含 findings / artifacts / 事件计数） |
| POST | `/api/v1/tasks/{task_id}/enqueue` | JWT | 是 | 重新入队 pending / failed / cancelled 任务 |
| POST | `/api/v1/tasks/{task_id}/cancel` | JWT | 是 | 协作式取消运行 / 排队中的任务 |
| POST | `/api/v1/tasks/{task_id}/delete` | JWT | 是 | 删除任务 |
| GET | `/api/v1/tasks/{task_id}/plan` | JWT | 是 | 获取任务规划 |
| GET | `/api/v1/tasks/{task_id}/live` | JWT | 是 | 实时状态快照（TaskLiveState） |
| GET | `/api/v1/tasks/{task_id}/events` | JWT | 是 | 任务事件流（历史，可回放） |
| GET | `/api/v1/tasks/{task_id}/usage` | JWT | 是 | 该任务令牌用量 |
| GET | `/api/v1/tasks/usage/summary` | JWT | 是 | 租户用量汇总 |

### 4.4 指令注入 `instruction.py`

| 方法 | 路径 | 鉴权 | 多租户 | 用途 |
|---|---|---|---|---|
| POST | `/api/v1/tasks/{task_id}/instructions` | JWT | 是 | 向运行中任务追加指令 |

### 4.5 实时流 `stream.py`

| 方法 | 路径 | 鉴权 | 多租户 | 用途 |
|---|---|---|---|---|
| GET | `/api/v1/tasks/{task_id}/stream` | JWT | 是 | SSE 实时事件流 |

### 4.6 持久化查询 `persistence.py`

| 方法 | 路径 | 鉴权 | 多租户 | 用途 |
|---|---|---|---|---|
| GET | `/api/v1/persistence/tasks/{task_id}` | JWT | 是 | 任务完整详情 |
| GET | `/api/v1/persistence/tasks/{task_id}/events` | JWT | 是 | 事件明细 |
| GET | `/api/v1/persistence/tasks/{task_id}/findings` | JWT | 是 | 发现项 |
| GET | `/api/v1/persistence/tasks/{task_id}/artifacts` | JWT | 是 | 产物文件 |
| GET | `/api/v1/persistence/tasks/{task_id}/audit` | JWT | 是 | 审计事件 |

### 4.7 审批网关 `approval.py`

| 方法 | 路径 | 鉴权 | 多租户 | 用途 |
|---|---|---|---|---|
| GET | `/api/v1/approval/requests` | JWT | 是 | 待审批列表 |
| GET | `/api/v1/approval/requests/{request_id}` | JWT | 是 | 审批详情 |
| POST | `/api/v1/approval/requests/{request_id}/decision` | JWT | 是 | 通过/拒绝决策 |

### 4.8 报告导出 `report.py`

| 方法 | 路径 | 鉴权 | 多租户 | 用途 |
|---|---|---|---|---|
| GET | `/api/v1/report/{task_id}` | JWT | 是 | HTML 报告 |
| GET | `/api/v1/report/{task_id}/markdown` | JWT | 是 | Markdown 报告 |
| GET | `/api/v1/report/{task_id}/json` | JWT | 是 | JSON 结构化报告 |

### 4.9 系统运维 `system.py`

| 方法 | 路径 | 鉴权 | 多租户 | 用途 |
|---|---|---|---|---|
| GET | `/api/v1/system/worker-status` | Admin | 否 | Worker 健康 |
| POST | `/api/v1/system/task-reconcile` | Admin | 否 | 任务对账（清理幽灵任务） |
| GET | `/api/v1/system/kali-status` | Admin | 否 | 沙箱状态 |
| GET | `/api/v1/system/llm-status` | Admin | 否 | LLM 连通性 |
| POST | `/api/v1/system/probe` | 公开 | 否 | 端到端链路验证（健康检查探测） |

### 4.10 定价 `pricing.py`

| 方法 | 路径 | 鉴权 | 多租户 | 用途 |
|---|---|---|---|---|
| GET | `/api/v1/pricing/config` | JWT | 是 | 读取令牌定价 |
| PUT | `/api/v1/pricing/config` | Admin | 否 | 更新定价 |

### 4.11 API Token `api_tokens.py`

| 方法 | 路径 | 鉴权 | 多租户 | 用途 |
|---|---|---|---|---|
| POST | `/api/v1/api-tokens/` | JWT | 是 | 创建 PAT |
| GET | `/api/v1/api-tokens/` | JWT | 是 | 列出 PAT |
| GET | `/api/v1/api-tokens/{token_id}` | JWT | 是 | 令牌详情 |
| DELETE | `/api/v1/api-tokens/{token_id}` | JWT | 是 | 吊销 PAT |

---

## 5. 平台 API Key 与环境变量清单

来源：仓库根目录 `.env`（**已 gitignore，不入库、仅本地**）。以下仅保留前缀/占位，不暴露完整密钥。

| 变量名 | 用途 | 归属/平台 | 是否入库 |
|---|---|---|---|
| `OPENAI_API_KEY` | LLM 调用密钥 | 火山方舟 Ark（OpenAI 兼容接口，经百度 agent-awd 网关） | 否（仅 `.env`） |
| `OPENAI_BASE_URL` | LLM 网关地址 | `https://agent-awd.baidu.com/v1` | 否 |
| `POBI_V2_MODEL` | 模型标识 | `openai/glm-5.2-agent-chanllenge` | 否 |
| `POBI_API_TOKEN` | 平台访问令牌 | 自有平台（前缀 `pk_3758d20e_...`） | 否 |
| `POBI_V2_TOKEN_ENCRYPTION_KEY` | 令牌加解密密钥 | 平台 | 否 |
| `BENCHMARK_TOKEN` | 基准平台评测令牌 | `tsecbench.zc.tencent.com`（前缀 `a4284b71-...`） | 否 |
| `BENCHMARK_BASE_URL` | 基准平台地址 | 腾讯安全基准平台 | 否 |
| `JWT_SECRET` | JWT 签名密钥 | 平台 | 否 |
| `POBI_V2_ADMIN_EMAIL` / `POBI_V2_ADMIN_PASSWORD` | 管理员凭据 | 平台 | 否 |
| `POBI_V2_SANDBOX_IMAGE` | 沙箱镜像 | `xoxruns/sandboxed_kali:latest` | 否 |
| `POBI_V2_SANDBOX_NETWORK` | 沙箱网络模式 | `host` | 否 |
| `POBI_HOME` / `POBI_CACHE_HOME` | 工作区/缓存根 | 本地 `/root/.pobi_v2` | 否 |
| `POBI_V2_WORKER_MAX_JOBS` | 单 Worker 并发任务数（ARQ `max_jobs`） | 平台（默认 2） | 否 |
| `POBI_V2_WORKER_REPLICAS` | Worker 副本数（docker-compose `deploy.replicas`） | 平台（默认 1） | 否 |
| `POBI_V2_LLM_MAX_CONCURRENCY` | LLM 调用并发信号量上限 | 平台（默认 8） | 否 |
| `POBI_V2_AUTO_APPROVE` | 安全护栏开关 | `true`（自动批准） | 否 |
| `POBI_V2_MAX_TASK_DURATION_MINUTES` | 任务超时 | `60` 分钟 | 否 |

> 安全约定：所有密钥均在 `.env`，由 `python-dotenv` 加载；仓库 `.gitignore` 已忽略；注意不要将真实值写入任何文档或提交。

---

## 6. 技术约束与开发规范（必读）

遵循根目录 `AGENTS.md`。要点：

- **安全红线**：高危操作默认 fail-closed；`ScopePolicy` 授权闸门不可绕过；审批回调缺失时一律拒绝。
- **极简设计**：少即是多，函数职责单一、自解释；优先标准库，显式捕获异常。
- **多租户隔离**：所有数据接口必须按 `tenant_id` 隔离，禁止跨租户读取。
- **Nginx**：更新 `nginx.conf` 必须开启 Gzip，并对 SSE 路径关闭代理缓冲。
- **文档语言**：生成内容与 Artifacts 用中文；提交信息用客观工程化表述，禁止主观营销词汇。
- **破坏性操作**（删容器 / `git reset --hard` / `git clean -fd` 等）须先说明影响并获批。
- **许可证**：AGPL-3.0，网络服务须提供源码获取途径（见 `LICENSE` / `NOTICE`）。

---

## 7. 快速上手（开发）

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

### 7.1 端到端流程

1. `POST /api/v1/targets` 创建授权目标（填写 `in_scope` / `out_of_scope`）。
2. `POST /api/v1/tasks` 创建任务（自动校验授权范围 -> 入队）。
3. 前端订阅 `GET /api/v1/tasks/{task_id}/stream` 实时查看 Agent 思考与工具调用。
4. Worker 执行完成后，任务状态流转为 `completed` / `failed`，结果写入 `Task.result`。

> 沙箱工具（Docker / Playwright / AVFS）需要相应环境。M8 主路径默认驱动 `DeadEndAgent`，无 Docker 时回退 M7 `ScanWorkflow`（见 `engine/executor.py`）。

### 7.2 链路健康探测（probe 快路径）

链路验证不依赖多智能体，走轻量快路径：

1. `POST /api/v1/system/probe`（`target_id` 必填）立即返回 `task_id`，任务异步在共享 Kali 沙箱执行 `curl` 连通性探测 + 单次 LLM 结论。
2. Worker 由 `engine/probe_runner.py` 直接驱动（probe 自身绕过 avfs / DeadEndAgent 多智能体链路，仅做一次连通性探测），硬超时 90s 由 `asyncio.wait_for` 保证，通常数秒返回。
3. 结果经 `GET /api/v1/tasks/{task_id}` 或 SSE 拉取（`status=completed` 时 `result` 含结论）。
4. 前端「健康检查」页封装了三段实时状态（Worker / Kali / 模型）与「发起健康探测」按钮，并展示上一次探测结论。

> 生产 `docker-compose.override.yml` 已去掉 Worker 的 `--watch`（文件变动触发重启会卡死初始化），改后端的源码需手动 `docker compose restart worker` 生效。

---

## 8. 生产部署

镜像构建采用 **多阶段 Dockerfile**（`Dockerfile.prod`），编译工具与运行时彻底隔离。

### 8.1 配置环境变量

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

# ---- 并发（新增）----
POBI_V2_WORKER_MAX_JOBS=2               # 单 Worker 并行任务数（ARQ max_jobs）
POBI_V2_WORKER_REPLICAS=1               # Worker 副本数（docker-compose deploy.replicas）
POBI_V2_LLM_MAX_CONCURRENCY=8           # LLM 调用并发信号量上限
```

### 8.2 构建并启动

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

### 8.3 服务说明

| 服务 | 镜像 | 职责 |
|------|------|------|
| `postgres` | `${ACR_REGISTRY}/postgres:16-alpine` | 主数据存储 |
| `redis` | `${ACR_REGISTRY}/redis:7-alpine` | ARQ 队列 + 事件总线 + 取消状态 |
| `api` | `pobi_v2:1.0.0` | FastAPI（gunicorn+uvicorn），提供 `/api/v1` 与 SSE |
| `worker` | `pobi_v2:1.0.0` | `arq pobi_v2.engine.worker.WorkerSettings`，异步执行扫描（每 5min 自动对账收敛幽灵任务）；可经 `deploy.replicas` 横向扩容 |
| `web` | `${ACR_REGISTRY}/nginx:1.27-alpine` | 静态 SPA 托管 + 反代 + Gzip |

### 8.4 Gzip 与 SSE

`nginx.conf` 已强制开启 Gzip（防止明文传输大文件），并对 `/api/v1/tasks/` 关闭代理缓冲，
保证 SSE 事件逐条实时推送（见 AGENTS.md「更新 Nginx 配置必须开启 Gzip」）。

### 8.5 白盒代码分析（可选）

`pobi_agent.code_indexer.SourceCodeIndexer` 依赖 Playwright / Embedder 后端 / RAG，属重量级
可选能力。当前默认关闭，由 `engine/scan_workflow.resolve_whitebox_stage(enabled=False)`
控制；依赖齐备后启用，缺失时优雅降级为纯黑盒扫描，不阻断主流程。

### 8.6 许可证与源码提供义务（AGPL-3.0）

本项目以 **AGPL-3.0** 发布（详见 `LICENSE` 与 `NOTICE`）。依据 AGPL 第 13 条，若以网络服务
形式向公众提供，须向用户提供对应**完整源代码**获取途径。建议在页面页脚提供仓库链接，
或按 `NOTICE` 说明的途径提供源码归档。内嵌的 `pobi_agent/` 衍生自上游 pobi，其版权与
许可证见 `pobi_agent/THIRD_PARTY_NOTICE.md`。

### 8.7 发布流程（AGENTS.md 规范）

1. 本地源码修改验证通过后，更新 `version.txt` 与 `pyproject.toml` 版本号。
2. 检查 `Dockerfile.prod` 符合多阶段构建规范。
3. 提交：`git commit`（采用工程化客观表述，禁止主观营销词汇）。
4. Push 前确认是否打 Tag（`v*`）触发自动构建。
5. 部署前确认最新镜像已就绪，再执行 `start-prod.sh`（或 `docker compose up -d`）。

### 8.8 已收敛的监控遗留项（历史监控报告 A–D）

基于 `scripts/` 下监控数据，以下基础设施阻塞已修复（详见上文「核心能力」）：

- **事件可观测性**：持久化事件类型由 7 类扩至 17 类，`/live` 聚合最近事件，`/events` 支持回放，修复 SSE 断连即丢。
- **认证死循环**：`wait_for_auth_success` 假阳性已修复，`authenticate_service` 增加 `validated` 真实校验与连续失败熔断（报告 A/B：DVWA 登录 33 分钟死循环）。
- **结构性受阻**：提示词新增「结构性受阻」停止类别（框架 / 工具缺陷），避免无效重试。
- **协作式取消**：取消后主动移除 ARQ 队列幽灵 job + 子 Agent 子超时 + Worker 自动对账（报告 C：取消后任务仍处于 running）。
- **AVFS 命名空间**：`memory_session_id` 统一挂载 / 访问命名空间，消除 131 次「memory is not mounted」阻塞（报告 D）。
- **function-call 序列化**：`parse_browser_steps` 兼容字符串化 steps，消除嵌套 function-call 解析失败。

### 8.9 模型路由：按场景分流不同大模型（M9 预研）

现状：`Task.model` 已支持任务级覆盖模型（`deadend_runner` 取 `task.model or settings.model`），但内核 `AgentRunner` 仅接收单个 `ModelSpec`，任务内所有子 Agent 共用同一模型。需求：同一任务内，生成 payload 等敏感输出应走**本地无审核模型**，规划 / 推理仍走**云端有审核模型**。

改造路线（4 步）：

- **M9-1 平台配置支持角色前缀**：`pobi_v2/llm/config.py` 的 `get_model_spec` 增加 `role` 参数，按 `POBI_V2_MODEL_{ROLE}` / `POBI_V2_LLM_{ROLE}_API_KEY` / `POBI_V2_LLM_{ROLE}_API_BASE` 解析（如 `PAYLOAD` 角色指向本地无审核端点），缺省回退 `POBI_V2_MODEL`。仍保持单一解析入口。
- **M9-2 引入 `ModelRouter`**：内核新增薄封装 `ModelRouter(default, roles)`，提供 `for_role(name)`；`deadend_runner` 构造 `ModelRouter` 注入 `DeadEndAgent`，替代裸 `ModelSpec`。
- **M9-3 `AgentRunner` 按角色取模型**：`pobi_agent/agents/factory.py` 的 `AgentRunner.__init__` 兼容 `ModelRouter`，实例化 `CoreAgent` 前以 Agent 名称取对应 `ModelSpec`。按 Agent 名字自动分流，改动最小。
- **M9-4 标记 payload 生成角色**：确认内核中产出 payload 的子 Agent（shell / python_interpreter 等），绑定 `payload` 角色；可选补前端"任务可选模型"UI。

约束：所有模型仍经 LiteLLM 统一路由与计费（token 用量真实、价格按内置表估算）；本地模型 `api_base` 走内网，凭证与云端隔离；云端 `ContentPolicyViolationError` 不入本地 payload Agent。

---

*本文件随代码演进维护。删除或重大调整里程碑时，请同步更新第 2 节与构建路线。后续 Agent 直接读 `README.md` 与本文件即可掌握全貌。*
