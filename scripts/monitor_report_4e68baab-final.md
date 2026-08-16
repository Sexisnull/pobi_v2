# Agent 执行监控终态报告：接口为何不能反映真实工作状态

- 任务 ID：`4e68baab-1cc9-430d-b475-15674f183242`
- 任务名：dvwa测试
- 目标：`http://122.51.72.186:8081`（DVWA v1.10，账号 admin/password，目标查 SQL 注入）
- 运行模式：`yolo` / max_turns=50
- 最终状态：`cancelled`（用户取消，原运行约 50 分钟）
- 监控方式：API `/live` + `/plan` 轮询（357 次）+ Redis 事件流 `pobi_v2:events:{task_id}`（396 条）+ Worker 日志 `pobi_v2-worker-1`（1091 行）
- 报告生成：2026-08-16

---

## 〇、一句话结论

**接口"看不到 agent 在做什么"，不是 UI 没做好，而是数据层就把 agent 的工作过程过滤掉了**：事件总线只把 4 类"骨架事件"落库并对外暴露，而承载"思考/LLM 输入输出/工具调用/错误"的 396 条明细事件只活在 Worker 内存日志和一次性的 Redis/SSE 流里，断连即丢、无法回看。结果就是用户面对的接口，是一个"只有谁开始/结束、到了哪一步"的空壳。

---

## 一、问题清单（5 个）

### 问题 1：事件落库白名单过窄 —— 明细事件全部丢失
API（/live）只暴露 **4 类事件**：`plan_step / phase_changed / agent_start / agent_end`。而 `agent_thought / llm_response / llm_input / llm_iteration / log` 这些"agent 在想什么、做了什么"的核心事件**不落库、不暴露**。

### 问题 2：工具调用细节被截断，且窗口上限挤掉
`tool_call_start/end` 即便落库，参数/结果也被截断到 **200 字**；且 `/live` 的 `recent_events` 是**固定 30 条窗口**，高频的 `agent_start/end` 会把工具调用事件挤出窗口，用户连仅有的骨架工具调用都常看不到。

### 问题 3：API 状态更新严重滞后于真实进展
接口只在 **agent 切换 / 阶段推进** 这种骨架节点才变化。中间 agent 经历"7 次 LLM 迭代 + 连续工具报错"的错误循环时，接口长时间显示同一个 `agent`，用户完全不知道它正在反复失败重试。

### 问题 4：`agent_work` 思考/置信度字段恒为 null
所有 agent 的 `thought_summary` 与 `confidence_score` 在 API 中始终为 `None`，用户无法得知 agent 当前意图与把握。

### 问题 5：API 任务记录"冻结"——`updated_at` 不刷新
任务运行中 `updated_at` 定格在 `06:58:07Z`，此后 worker 持续推进 40+ 分钟，API 侧 `updated_at` 与事件窗口完全静止，直到用户取消那一刻才更新。**用户无法从接口判断任务是否还活着**，看起来像卡死。

---

## 二、证据（数据对比）

### 证据 A：API 实际暴露 vs Worker 真实发生（同一任务）

| 维度 | API `/live` 给人看的 | Worker 日志真实发生 |
|---|---|---|
| 事件类型 | 仅 4 种骨架（plan_step/phase_changed/agent_start/agent_end） | 含 agent_thought/llm_*/tool_*/log 等 10+ 种 |
| `recent_events` 数量 | 恒为 **30**（窗口上限） | Redis 全量 **396 条**，Worker **1091 行** |
| 工具调用 | 几乎不可见（被窗口挤出） | **91 次** `Tool Call` |
| 工具错误 | 不可见 | **34 次** `Tool Error` |
| LLM 思考/响应 | 不可见 | **383 条** |
| agent 思考摘要 | 全部 `None` | 62 条 `agent_thought`（含完整推理） |

### 证据 B：Redis 全量事件类型分布（API 看不到的部分）

| 事件类型 | 是否 API 可见 | Redis 捕获数 |
|---|---|---|
| `agent_end` | ✅ 是 | 26 |
| `agent_start` | ✅ 是 | 26 |
| `agent_thought` | ❌ 否 | 62 |
| `llm_input` | ❌ 否 | 88 |
| `llm_iteration` | ❌ 否 | 88 |
| `llm_response` | ❌ 否 | 62 |
| `log` | ❌ 否 | 27 |
| `task_created` | ❌ 否 | 6 |
| `task_expanded` / `task_status_changed` | ❌ 否 | 1 / 1 |
| `phase_changed` / `plan_step` | ✅ 是 | 1 / 8 |

→ **API 暴露的 4 种 vs Redis 中存在但 API 完全看不到的 8 种**，明细事件占比约 71%（285/396）。

### 证据 C：API 实测返回（节选）
```
recent_events 总数: 30
类型分布: {'phase_changed':1,'plan_step':9,'agent_start':11,'agent_end':9}
各 agent thought_summary / confidence_score: 全部 None
```

### 证据 D：Worker 反复失败但接口无感（典型片段）
- authenticator 经历 `Iteration 3→7`，连续报错：
  `authenticate() got an unexpected keyword argument 'success_substring'`
  `'str' object has no attribute 'items'`
  接口在此期间一直显示 `authenticator / running`，无任何错误提示。
- 全任务 **131 次** `AVFS workspace 'memory' is not mounted` 报错（基础设施阻塞），接口零提示。
- 接口 `updated_at` 冻结于 `06:58:07Z`，而 worker 在 `15:26` 仍在进行 `authenticator iteration 3` 推理——冻结超 25 分钟。

### 证据 E：工具调用彻底不可见（脚本采集偏差说明）
自动报告的"Redis 工具调用 start/end=0"是因为 `tool_call_start/end` 在 Redis 流中**未被 emit**（只 emit 了上述 10 类），进一步证实工具调用事件既不在 Redis 也不在 API——Worker 日志里的 91 次 `Tool Call` 是**唯一**留存处，且随进程结束/滚动而丢失。

---

## 三、根因定位

代码侧根因在事件总线（`pobi_v2/engine/event_bus.py`）的 `persist_event_worker`：其只对 7 类事件执行 `TaskEvent` 落库，且 `get_task_live`（`pobi_v2/routers/tasks.py`）对 `tool_call_start/end` 的 args/result 做了 200 字截断、对 `recent_events` 设了固定窗口。其余事件（含全部思考/LLM/工具明细）只经 Redis pub/sub 推送给在线 SSE 客户端，不持久化。

此外任务 `updated_at` 仅在状态机切换/取消时写库，运行期每个 agent 迭代不触发任务记录更新，造成"冻结"假象。

---

## 四、解决方案

### 方案 1（核心）：扩大事件落库白名单，新增明细事件表
- 在 `persist_event_worker` 中新增落库类型：`agent_thought / llm_response / llm_input / llm_iteration / agent_routed / validation_result / confidence_update / log / tool_call_start / tool_call_end`。
- 为控制主表体积，新建 `task_event_detail` 表（或 JSONB 列），与 `TaskEvent` 用 `task_id + seq` 关联，专存明细，避免撑大骨架事件表。
- 同步在 Redis pub/sub 中 emit `tool_call_start/end`（当前缺失）。

### 方案 2：去掉截断、提升窗口，或前端折叠
- `get_task_live` 中 `tool_call_start/end` 的 args/result 截断上限从 200 字提到合理值（如 2000），或存原文、前端折叠展示。
- `recent_events` 窗口改为按类型混合保留（如各类型保留最近 N 条），避免高频 `agent_start/end` 把工具/思考事件挤出。

### 方案 3：暴露"错误循环"信号
- 把 `tool_error` 作为独立落库事件，并在 `/live` 暴露 `last_error`、`retry_count`、`current_iteration`。
- 前端对"同一工具连续 N 次失败"给出醒目提示，让用户看到 agent 正在失败重试而非卡死。

### 方案 4：修复 `updated_at` 冻结
- 在 `persist_event_worker` 每次落库时（或每 N 次迭代）顺带 `touch` 任务 `updated_at`，使接口能反映"任务仍活跃"。
- 或新增 `/live` 派生字段 `last_event_at`（最近一次事件时间），前端据此显示"最后活跃 Xs 前"。

### 方案 5：新增"实时事件回放"接口
- 提供 `GET /tasks/{id}/events?type=&after_seq=` 从 `task_event_detail` 读取历史全量，供控制台"时间线"回看，弥补 SSE 断连即丢的缺陷。
- 控制台加"思考流"面板，按 `llm_iteration` 折叠展示 `agent_thought / llm_response / llm_input`，真正呈现"agent 在做什么"。

### 方案 6（顺带修复真实 Bug）：AVFS 工作区挂载
证据显示 131 次 `AVFS workspace 'memory' is not mounted` 是贯穿全程的基础设施阻塞，导致所有实时 HTTP agent 失效。建议单独排查 `avfs_mount` 工具注册与挂载流程——这虽非接口问题，但是 agent 实际跑不通的根因之一，应在修接口的同时修复。

---

## 五、监控脚本与产物

- 监控脚本：`scripts/monitor_agent.py`（三数据源并行采集 + 自动报告）
- 原始自动报告：`scripts/monitor_report_4e68baab-1cc9-430d-b475-15674f183242.md`（含完整 396 条事件时间线）
- 中间分析：`scripts/monitor_analysis_4e68baab.md`（5 缺陷速览）
- 本终态报告：`scripts/monitor_report_4e68baab-final.md`

> 下一步建议：按方案 1~5 改动 `event_bus.py` 与 `tasks.py`，并新增 `/events` 回放接口；方案 6 另立 issue 排查 AVFS 挂载。需要我直接动手改代码可随时告知。
