# 任务手动终止问题记录与解决方案

> 生成日期：2026-08-16
> 关联任务：`4e68baab-1cc9-430d-b475-15674f183242`（"dvwa测试"，目标 `122.51.72.186:8081`）
> 关联文档：`SLOW_TASK_ANALYSIS.md`（卡顿根因）、`AUTHENTICATOR_DVWA_FAILURE_ANALYSIS.md`（登录失败根因）

## 1. 背景

目标任务是一个 DVWA SQL 注入测试任务，因登录环节死循环卡死约 33 分钟（详见
`SLOW_TASK_ANALYSIS.md`）。用户决定手动终止该任务。终止过程中暴露了框架在
"协作式取消 + 幽灵任务"上的若干问题。

## 2. 问题

### 2.1 `POST /tasks/{id}/cancel` 返回 200，但任务未真正停止

调用 `POST /api/v1/tasks/4e68baab-.../cancel` 返回 HTTP 200，但任务 `status`
仍保持 `running`，`cancel_requested` 字段仍为 `None`（复查多次均如此）。

**根因**：取消是**协作式（cooperative）** 的——`cancel_task` 端点仅向
`cancel_state` 写入标志（进程内 set 或 redis），真正终止依赖 worker 在
`_run_task_body` 循环迭代间的 `is_cancelled(tid)` 检查点
（`pobi_v2/engine/executor.py:282`）。当任务卡在 LLM 登录死循环、worker 协程
长时间被占用（或任务已变成"幽灵任务"——worker 实际已不再执行它），该检查点
永远到不了，协作式取消失效。

### 2.2 任务变成"幽灵任务"：前端显示 running，worker 已不执行

`worker-status` 显示 `j_ongoing=0`（当前无 job 执行）、`queue_depth=1`，但
任务 `status=running` 持续数分钟不变。这正是 `executor.py:108-109` 注释预警的
"Worker 已丢弃、前端仍显示 running"的幽灵任务——DB 状态与 worker 真实状态脱节。

### 2.3 `worker-status` 的 `queue_depth` 统计陈旧

即使任务已被强制终止、任务列表已无活跃任务，`worker-status` 仍报
`queue_depth:1`，其 `stats.detail` 时间戳停留在任务启动时的 `06:55:26`，
计数器未随任务状态变化刷新。属**僵尸计数**，不代表真实队列仍有任务。

## 3. 证据（实测数据）

### 3.1 验证 apikey 有效（排除鉴权问题）

用同一 apikey 调用多个需鉴权的端点，均返回 200：

| 接口 | HTTP |
|---|---|
| `GET /api/v1/tasks` | 200 |
| `GET /api/v1/targets` | 200 |
| `GET /api/v1/pricing` | 200 |
| `GET /api/v1/system/worker-status` | 200 |

（首次调用偶发 401 为瞬时抖动，重试稳定 200。）

### 3.2 定位目标任务

`GET /api/v1/tasks` 列表第 1 条即目标：

```
id=4e68baab-1cc9-430d-b475-15674f183242  status=running
name="dvwa测试"  created=2026-08-16T06:58:06Z
```

### 3.3 cancel 无效的证据

- `POST /tasks/{id}/cancel` → 200，但复查 `status=running`、`cancel_requested=None`
- `worker-status`: `j_ongoing=0`、`queue_depth=1`
- 日志 `requester.jsonl` 修改时间停在 `15:46:14`，`context.txt` 停在 `15:44:38`
  （说明任务进程仍在产出，cancel 未中断执行层）

### 3.4 终止成功后，worker 确已停止的证据

调用 `POST /api/v1/system/task-reconcile` 后，再次核查：

| 信号 | 观测值 | 含义 |
|---|---|---|
| 任务 `status` | `cancelled` | 终态已落库 |
| `finished_at` | `2026-08-16T07:46:58Z` | 终止时间 |
| `error` | `检测到取消请求，自动终止` | reconcile 终局原因 |
| 任务事件流 | 76 条，最后时间 `07:44:45Z` | 事件流停止增长 |
| 到 `122.51.72.186` 的连接 | **无** | worker 不再打目标 |
| 日志修改时间 | `requester.jsonl` 停 `15:46:14` | 执行层已停写 |
| 活跃任务数（全列表） | **0** | 无 pending/queued/running 任务 |

`j_ongoing=0` + 无目标连接 + 日志停写 + 活跃任务数=0 四点一致，确认 **worker 已
真正停止，无遗留任务在跑**。

## 4. 解决方案（实际操作步骤）

### 4.1 第一步：调用标准取消接口

```bash
curl -X POST -H "Authorization: Bearer <APIKEY>" \
  http://localhost:8000/api/v1/tasks/<TASK_ID>/cancel
```

若任务响应及时（worker 能跑到检查点），此步即可终止。

### 4.2 第二步（本任务所需）：调用 task-reconcile 强制对账

当标准 cancel 无效（幽灵任务 / worker 阻塞）时，调用：

```bash
curl -X POST -H "Authorization: Bearer <APIKEY>" \
  http://localhost:8000/api/v1/system/task-reconcile
```

该端点（`pobi_v2/routers/system.py:121`）逐条扫描活跃任务：
- 若 `is_cancelled(task.id)` 为真 → 直接标 `cancelled`（`system.py:152`）
- 若 `running` 且 `started_at` 超过 `JOB_TIMEOUT` → 标 `failed`（超时幽灵任务）

本任务正是走到第 152 行分支被强制终止。

### 4.3 第三步：多维度验证 worker 真停（关键，不可只看前端）

前端显示 `cancelled` 只代表 DB 状态更新，**不代表 worker 真停**。必须核查：

1. `GET /api/v1/tasks` 全列表，`pending/queued/running` 状态任务数应为 0；
2. `GET /api/v1/system/worker-status` 的 `j_ongoing` 应为 0；
3. 本机无任何到目标主机的活跃网络连接（`lsof -i | grep <目标IP>`）；
4. 任务 session 日志（`~/.pobi_v2/cache/logs/<agent_id>/<session_id>/`）与
   `run_context/context.txt` 修改时间停止推进。

四点一致方可确认 worker 已停止。

## 5. 框架层待修复项（治本）

1. **协作式取消不可靠**：worker 被长耗时 LLM 循环占用时，`is_cancelled`
   检查点到不了。建议引入**信号/超时中断**（如 ARQ 的 `job_timeout` 强制
   `CancelledError`，或定期 `asyncio.sleep(0)` 让出控制权），使取消能及时生效。
2. **幽灵任务防护**：`executor.py` 已有 `try/finally` 兜底，但需在 worker
   心跳超时后主动把 `running` 标为 `failed/cancelled`，避免前端永显 running。
3. **`worker-status` 统计陈旧**：`queue_depth` 等计数器未随任务状态刷新，
   应基于真实队列（ARQ/redis）重新计算，避免误导运维判断。
4. **取消后清理队列项**：cancel 成功时应同步从 ARQ 队列移除该 job，避免
   `queue_depth` 虚高。

> 注：上述治本项与 `SLOW_TASK_ANALYSIS.md` 第 7、8 节的"结构性受阻熔断 +
> 自主终止"机制互补——即便取消可靠，也应从源头防止任务陷入需手动终止的死循环。

## 6. 涉及源码位置

| 位置 | 作用 |
|---|---|
| `pobi_v2/routers/tasks.py:214` | `cancel_task` 端点（协作式取消） |
| `pobi_v2/engine/cancel_state.py:65` | `request_cancel` / `is_cancelled`（进程内/redis 标志） |
| `pobi_v2/engine/executor.py:282` | worker 协作式取消检查点 |
| `pobi_v2/engine/executor.py:108-156` | 任务终态兜底（幽灵任务防护注释） |
| `pobi_v2/routers/system.py:121` | `task-reconcile` 强制对账端点 |
| `pobi_v2/routers/system.py:152` | reconcile 中 cancel 标志优先分支 |
| `pobi_v2/db/models.py:201` | `Task.cancel_requested` 字段 |
