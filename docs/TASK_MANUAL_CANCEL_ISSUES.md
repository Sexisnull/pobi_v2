# 任务手动终止问题记录与解决方案

> 生成日期：2026-08-16（原始实测），2026-08-18 复核更新
> 关联任务：`4e68baab-1cc9-430d-b475-15674f183242`（"dvwa测试"，目标 `122.51.72.186:8081`）

## 0. 复核结论（2026-08-18）

原始记录将本次手动取消判定为「cancel 无效→需 task-reconcile 兜底」。复核实测数据后
**修正结论**：取消链路本身有效——任务经 `task-reconcile` 进入 `cancelled` 终态后，
事件流（76 条）仍完整记录、到目标的连接归零、worker `j_ongoing=0`、**可正常重跑**。
「cancelled 但事件仍在」是**正常保留**，不是失败或数据泄漏。原始文档把「事件仍在」
误读为「失败/漏洞」是误判。

当前状态：**部分解决**。标准协作式 `cancel` 对「卡在长耗时 LLM 循环」的任务可能到不了
检查点，仍需 `task-reconcile` 兜底；但兜底后状态、事件、重跑均正常，无数据一致性问题。

## 1. 背景

目标任务是一个 DVWA SQL 注入测试任务，因登录环节死循环卡死约 33 分钟。用户手动终止
后，文档记录了框架在「协作式取消 + 兜底对账」上的行为。

## 2. 行为机制（实际）

### 2.1 协作式取消
`POST /tasks/{id}/cancel` → `cancel_state.request_cancel`（进程内/redis 标志）。
worker 在 `_run_task_body` 循环迭代间经 `is_cancelled(tid)` 检查点生效。任务被 LLM
长循环占用时，检查点可能延迟到达。

### 2.2 兜底对账 `task-reconcile`
`POST /api/v1/system/task-reconcile` 扫描活跃任务：
- `is_cancelled` 为真 → 标 `cancelled`；
- `running` 且超 `JOB_TIMEOUT` → 标 `failed`（超时幽灵任务）。

### 2.3 取消后的正确性（已验证）
任务进入 `cancelled` 后：状态落库、事件流停止增长（**历史事件保留，非丢失**）、
到目标连接归零、`j_ongoing=0`、可对该目标发起新任务重跑。无幽灵任务或数据残留。

## 3. 框架层待修复项（治本，仍有效）

1. **协作式取消在长 LLM 循环内延迟**：建议引入超时中断（如 `job_timeout` 强制
   `CancelledError` 或定期让出控制权），使取消及时生效，减少对 `task-reconcile`
   兜底的依赖。
2. **`worker-status` 统计陈旧**：`queue_depth` 等计数器未随任务状态实时刷新，
   应基于真实队列（ARQ/redis）重算，避免误导运维。
3. **取消后清理队列项**：cancel 成功时应同步从队列移除该 job，避免 `queue_depth` 虚高。

> 注：源头治理见 `EVOLUTION_ROADMAP.md` 阶段 5（wall-clock 熔断 `POBI_MAX_RUNTIME_SECONDS`），
> 从源头防止任务陷入需手动终止的死循环。


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
