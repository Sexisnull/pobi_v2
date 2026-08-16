# Agent 监控中间分析：接口为何不能反映真实工作状态

- 任务 ID：`4e68baab-1cc9-430d-b475-15674f183242`
- 目标：`http://122.51.72.186:8081`
- 目标：`登录账号为 admin，password。检查目标存在的 sql 注入漏洞，其余不检查。`
- 运行模式：`yolo`
- 分析时间：2026-08-16（实时盯控阶段）
- 数据源：`API /live` + `Redis 事件流 pobi_v2:events:4e68baab-...` + `worker 容器日志 pobi_v2-worker-1`

## 一、实时对比证据（同一时间段）

| 维度 | API `/live` 给人看的内容 | Worker 日志真实发生 | Redis 流 |
|---|---|---|---|
| 状态 | `running` / `authenticator` / `recon` step `running` | authenticator 第 4→7 次 LLM 迭代，反复调 `authenticate` 并连续报错 | 6~12 秒内 0 条新事件 |
| 思考 / LLM | 完全无 | 有 `LLM Thinking`、`LLM Response`、`LLM Input - Tool Result`、`LLM Request(Iteration N)` | 无 |
| 工具调用 | 仅 `tool_call_start`（参数截断 200 字） | `authenticate` 调用 7+ 次，含完整入参/出参 | 仅 `tool_call_start/end` |

实测佐证：
- 独立订阅 Redis 频道 `pobi_v2:events:4e68baab-...` 6~12 秒，捕获事件数 = **0**。
- 同期 worker 日志显示 authenticator 处于 `Iteration 4→7` 的错误循环。
- API `recent_events` 在该时段纹丝不动；直到 agent 实际从 authenticator 切换到 requester，API 才更新 `current_agent`。

## 二、接口不能反映真实状态的根因（四个结构性缺陷）

### 缺陷 1：事件落库白名单过窄（根因）
`persist_event_worker`（`pobi_v2/engine/event_bus.py`）只把 **7 类事件**写入 `TaskEvent` 表：
`plan_step / phase_changed / agent_start / agent_end / tool_call_start / tool_call_end / report_task_event`。

而 agent 真实「在做什么」的主体事件——`LLM Thinking / LLM Response / LLM Input(Tool Result) / LLM Request(Iteration) / Tool Call / Tool Error / agent_thought / validation_result / confidence_update`——**根本不进事件总线/数据库**，只在 Worker 进程内存日志和一次性 SSE 流里出现，断连即丢失、无法回看。

### 缺陷 2：工具调用参数/结果被截断
`tool_call_start/end` 虽落库，但 `args`/`result` 字段在 `get_task_live` 中被截断至 **200 字**（`pobi_v2/routers/tasks.py` 第 418、423 行）。
Worker 日志可见 `authenticate` 的完整入参（`auth_flow: form/json`、`auth_type: session_cookie`）与完整报错（`got an unexpected keyword argument`、`'str' object has no attribute 'items'`），API 侧看不到。

### 缺陷 3：API 状态更新严重滞后于真实进展
- 真实：authenticator 经历 7 次迭代 + 多次 `Tool Error` 的错误循环后，才切换到 requester。
- 接口：极长时间内一直显示 `authenticator / running`；中间「卡在错误循环」对用户是**完全黑盒**，看不到正在反复失败重试。

### 缺陷 4：`agent_work` / 思考内容缺失，只有骨架
`agent_start` 仅含任务 prompt 前缀；`agent_end` 的 `thought_summary / confidence_score` 实测为 `null`。
用户无法得知 agent 当前在想什么、为何这样决策、对某步有多自信。

### 缺陷 5（实测新增）：API 任务记录「冻结」，updated_at 不刷新
- 任务在 API 的 `updated_at` 定格于 `2026-08-16T06:58:07Z`，而 worker 在 25+ 分钟后仍在持续推进（authenticator iteration 3、多次 agent 切换、工具调用）。
- 现象：`GET /tasks/{id}` 的 `status` 恒为 `running`、`updated_at` 不变；`/live` 的 `current_agent`/`current_phase` 偶有变化但 `recent_events` 数量恒定在 30（窗口上限）。
- 影响：**用户无法从 API 判断任务是否还活着**——看起来像卡死，实际 worker 在干活。这是比"内容少"更严重的信任危机。

## 三、一句话根因

> 事件总线的「落库白名单」把 agent 的工作过程（LLM 思考、工具调用细节、错误循环）全部过滤掉；接口只能呈现「哪个 agent 开始/结束、到了哪个阶段」的骨架。承载完整过程的事件既不在数据库，也不在 API，只在 Worker 内存日志与一次性 SSE 流里——断连即丢失、无法回看。

## 四、修复方向（供后续）

1. **扩大落库白名单**：在 `persist_event_worker` 中至少新增 `agent_thought / llm_response / llm_input / llm_iteration / agent_routed / validation_result / confidence_update / log`，并单独建一张 `task_event_detail` 表避免撑大主事件表。
2. **工具调用去截断/提上限**：把 `tool_call_start/end` 的 args/result 截断上限从 200 字提到合理值（如 2000），或存原文、前端折叠。
3. **暴露错误循环信号**：把 `tool_error` 作为独立事件落库并在 `/live` 暴露 `last_error / retry_count`，让用户看到 agent 正在失败重试。
4. **新增「实时事件回放」接口**：从 Redis 流或新表读取历史全量事件，供控制台「时间线」回看，弥补 SSE 断连即丢的缺陷。

---
*本文件为盯控阶段中间结论，终态完整对比报告见 `scripts/monitor_report_4e68baab-1cc9-430d-b475-15674f183242.md`。*
