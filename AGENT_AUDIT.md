# pobi_v2 Agent 扫描逻辑查漏补缺

> 结论先行：**原 pobi 的 agent 扫描逻辑并未被 pobi_v2 复刻**。pobi_v2 目前只复用了最底层的 `CoreAgent`（单轮 LLM 对话），而真正构成"扫描能力"的多 Agent 编排、工具注入、scope 闸门、confidence/validation、report 生成、取消/持久化联动等，均未落地或已断裂。

---

## 一、原 pobi agent 的核心扫描逻辑（应被复刻的部分）

原项目的扫描能力位于 `pobi/pobi_agent/`，核心不是单个 agent，而是一套编排框架：

| 能力 | 实现位置 | 说明 |
|------|----------|------|
| 顶层编排 | `agents/components/planner.py`、`agents/components/executor.py::AgentExecutor`、`agents/architecture.py::ADaPTAgent` | 任务分解、子任务树、深度受限的递归执行（max_depth） |
| 工作流编排器 | `pobi_agent.py::DeadEndAgent` | 串起 recon（threat_model）→ exploit（run_exploitation / ADaPT）→ report 三阶段 |
| 工具集 | `tools/browser_automation/pw_requester.py`、`tools/shell/*`、`tools/webapprecon/*` | HTTP 请求、命令执行、Web 侦察（被注入到 supervisor/exploit agent） |
| 授权范围闸门 | `scope.py::ScopePolicy.check` | **挂在 `pw_requester.py:584` 的网络出口处**，越权 URL 直接抛 `ScopeViolation`；启用时 fail-closed |
| 置信度/验证 | `agents/components/validation_strategies.py::ValidationGate` | 评估是否达成目标、产出 confidence、critique、validation_token |
| 报告生成 | `agents/reporter.py::ReporterAgent` | 调用 LLM + `write_workspace_file` 工具，把 Markdown 报告写到 AVFS `reports/` 目录 |
| 事件流 | `hooks.py::EventHooks` | 全局 `set_event_hooks`，覆盖 agent_start/thought/tool_call/confidence/validation/error 等 |
| 取消 | `DeadEndAgent.interrupt_workflow()` 置 `interrupted` 标志，主循环检查 | 协作式中断 |
| 记忆/上下文 | `context/context_engine.py`、`agents/generic_agents/memory_agent.py` | 持久化工作记忆、统一上下文 |
| 代码索引 | `models/registry.py`、`embedders/code_indexer.py` | 对目标爬取+向量化供检索 |

关键点：`DeadEndAgent` 通过 `prepare_dependencies()` 把 `requester_deps/shell_deps/webapprecon_deps` 注入到 `AgentExecutor`，而 `AgentExecutor.execute_supervisor` 内部把对应工具（`pw_requester`、`shell`、`webapprecon`）交给 LLM 调度。`scope.py` 默认读 `~/.cache/pobi/scope.yaml`，由 web console 写入——**scope 是工具侧强制的，不是 agent 提示词层的软约束**。

---

## 二、pobi_v2 当前的实现状态

`pobi_v2/pobi_v2/engine/`：

| 模块 | 现状 | 与原 pobi 对比 |
|------|------|----------------|
| `agent_adapter.py::build_pobi_agent` | **定义后从未被调用**（executor 用 `CoreAgent`） | 预留接口，未启用 |
| `executor.py::run_task` | 调 `CoreAgent(...).run()` 单轮 LLM | 无工具、无编排、无 scope、无 reporter |
| `event_bus.py::PobiV2EventHooks` | 实现完整 `EventHooks` 协议，安装到全局 | 接口对接 ✅，但事件因 session_id 不匹配而收不到 |
| `guardrails.py` | 自实现 `check_scope`（按 target.in_scope/out_of_scope 文本匹配） | 与原 `ScopePolicy` 类似但不自动挂到网络出口 |
| `cancel_state.py` + `is_interrupted` | 内存/redis 标志 | 机制有，但和 session_id 不匹配 |
| `approval.py` | 自实现高危工具审批 gate | 新增能力，原 pobi 也有 `approval_callback`（挂在 DeadEndAgent，但未在 executor 串联） |
| `report.py` | 自实现 `build_report`（聚合 findings/events/artifacts） | 与原 `ReporterAgent`（LLM 生成 Markdown）思路不同，是结构化汇总而非 LLM 叙述 |

---

## 三、关键断链（必须修复）

### 断链 1：session_id 不一致 → SSE 实时流、取消全部失效
- `executor.py` 调 `agent.run(prompt=..., deps=None)`，`CoreAgent` 在无 deps 时 `session_id="unknown"`。
- `stream.py` 按 `task_id` 订阅事件总线；`cancel_state` 按 `task_id` 存标志；但 `PobiV2EventHooks` 发布/查询都用 `session_id`。
- 结果：前端 `/tasks/{id}/stream` 永远收不到事件；`is_interrupted` 查的是 `"unknown"` 而非 `task_id`，取消无效。

**修复方向**：`run_task` 必须传 `deps=AgentDeps(session_id=task_id)`，并让 `is_interrupted` / `cancel_state` 都按 `task_id` 键对齐。

### 断链 2：真正的扫描编排 + 工具未接入
`CoreAgent` 默认无工具。`executor.run_task` 未注入任何工具，也未调用 `DeadEndAgent`/`AgentExecutor`，因此：
- 不会发 HTTP 请求、不会执行命令 → 不是"扫描"。
- `scope` 闸门（挂在 `pw_requester`）根本没机会触发。
- 无 confidence/validation 事件 → 置信度 UI 空转。
- 无 `ReporterAgent` 调用 → 报告只来自 pobi_v2 自实现的 `build_report`（无 LLM 叙述）。

**修复方向**（二选一）：
- **方案 A（推荐，完整复刻）**：让 `build_pobi_agent` 真正构造 `DeadEndAgent`，在 `run_task` 里跑 `threat_model / start_supervisor / start_testing_stream` 等阶段，把 `target` 解析进 scope，注入工具依赖。
- **方案 B（最小可用）**：给 `CoreAgent` 注入工具集（`pw_requester`→scope 自动生效、`shell`、`webapprecon`），并在 `AgentDeps` 中传入 `session_id=task_id`、scope 策略对象，使单层 agent 也能执行真实扫描。

### 断链 3：scope 不联动
- pobi_v2 `Target` 的 `in_scope/out_of_scope`（已改 JSONB）未传给 agent。
- 原 `ScopePolicy` 读 YAML 文件，与 pobi_v2 的 DB 目标完全隔离。
- 结果：即使接入 `pw_requester`，scope 闸门也因 YAML 未写而处于 `enabled=False`（no-op，放行一切）。

**修复方向**：在 `run_task` 启动时，把 `Target.in_scope/out_of_scope` 写一份临时 `ScopePolicy` 并 `enabled=True`，或让 `check_scope` 接受策略对象入参（`pw_requester` 已支持全局 `check_scope(url)`，需改用注入的策略）。

### 断链 4：report 与原 `ReporterAgent` 语义不同
- pobi_v2 `build_report` 是机械聚合（findings 列表 + 事件计数 + 产物），不含 LLM 生成的自然语言叙述与 PoC 证据。
- 原 `ReporterAgent` 由 agent 自己 `write_workspace_file` 写报告，且依赖 `AgentExecutor` 在 validation stop 时触发。

**修复方向**：在编排完成后调用 `ReporterAgent` 生成叙述性 Markdown，与 pobi_v2 的结构化 findings 合并返回。

### 断链 5：findings / artifacts 没有落库路径
- pobi_v2 建了 `Finding`/`Artifact`/`AuditEvent` 模型，但 `executor.run_task` 当前实现**未调用任何 `persistence.record_*`**（被简化掉了）。
- 原 pobi 的发现由 `AgentExecutor`/`ContextEngine` 累积到上下文，再由 reporter 落盘；pobi_v2 的 `persistence.py` 虽存在却未被 agent 事件触发。

**修复方向**：在 `PobiV2EventHooks` 中接 `task_status_changed`/`tool_call_end`/`validation_result` 等事件，写入 `TaskEvent`；并新增从 `ContextEngine` 统一上下文解析 `Finding`/`Artifact` 的落库逻辑（需要 agent 暴露结构化结果）。

---

## 四、查漏补缺清单（按优先级）

| 优先级 | 缺口 | 影响 |
|--------|------|------|
| P0 | 修复 `run_task` 传 `session_id=task_id` 的 deps | SSE/取消立即恢复 |
| P0 | 接入真实扫描编排：`DeadEndAgent` 或给 `CoreAgent` 注入工具 | 否则"扫描"不存在 |
| P0 | scope 与 `Target` 联动并 `enabled=True` | 安全护栏生效 |
| P1 | `persistence` 落库接入事件钩子（events/findings/artifacts） | 报告与审计有数据 |
| P1 | 调用 `ReporterAgent` 生成叙述性报告 | 报告可用性 |
| P2 | 多阶段编排（recon→exploit→report）暴露为任务子阶段 | 复刻原工作流 |
| P2 | 记忆/代码索引（`ContextEngine`/`MemoryAgent`/`CodeIndexer`） | 长期任务质量 |
| P2 | 审批 gate 真正串联 `AgentExecutor` 的高危工具调用 | 高危操作人工卡点 |

---

## 五、结论

pobi_v2 目前是一套**完整的后端骨架 + 前端 + 多租户鉴权 + 审批/报告接口**，但**agent 扫描引擎是空壳**：只挂载了 `CoreAgent`，缺少原 pobi 的核心扫描编排、工具注入、scope 强制闸门、confidence/validation、LLM 报告、以及事件→持久化的闭环。要让 pobi_v2 真正"能扫描"，需按上面 P0 三项打通——最小代价是方案 B（给 CoreAgent 注入工具 + 传 session_id + 注入 scope 策略），完整复刻则是方案 A（驱动 DeadEndAgent 三阶段编排）。
