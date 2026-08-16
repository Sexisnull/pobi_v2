# 任务 `f3d1d6f6-9c12-407e-8eeb-62fbd802bd84` 卡死问题诊断与修复方案

> 诊断时间：2026-08-16
> 诊断人：AI Agent（基于 worker 容器日志 + 数据库事件轨迹 + 源码静态分析）
> 任务状态：`running`（卡死，worker 进程存活但无新事件产出）

## 1. 摘要

任务从前端观察表现为"卡住"：状态长期停留在 `running`，事件流约自 `2026-08-16 09:18` 起不再更新，但后端 worker 进程存活（每 5 分钟 `_auto_reconcile` cron 正常执行）。

经排查，问题由**两层相互独立但相互叠加的缺陷**构成：

1. **根因 A（命名空间不一致）**：`executor.py` 的 `_persist_agent_summary` 在写 memory 工作区时使用 `self.session_id`（= `task_id`），而 memory 工作区在 `DeadEndAgent.__init__` 中是以 `str(agent_id)` 挂载的。两者 key 不一致，导致每次子 Agent 完成时持久化 summary 失败，抛出 `AVFS workspace 'memory' is not mounted. Call avfs_mount first.`，且该错误被吞掉，使 supervisor 误判"memory 初始化成功"。
2. **根因 B（命令无超时挂起）**：`sandbox.execute_command` 在 `stream=True` 且不传 `timeout_seconds` 时走 `container.exec_run(..., stream=True)` 路径，**无任何超时**。某个长时间/挂起的 shell 命令（疑似 `ffuf` 大字典爆破）在 streaming 模式下既不结束也不报错，导致 await 永久挂起，且无新事件产出 → 前端表现为"卡死"。

根因 A 并未直接卡死任务，但它让 supervisor 误判 memory 就绪并放任 shell agent 进入无人监管的长时间命令，最终触发根因 B。

## 2. 证据（作证材料）

### 2.1 环境与日志定位

- 本地磁盘 `logs/worker.log`、`logs/worker_run.log` **不包含本任务**（本地仅处理过 `c9689467...`，且 `logs/worker.log` 因 Redis 未启动而 worker 启动失败）。
- 任务实际运行于 docker 容器 `pobi_v2-worker-1`（状态 `Up 43 minutes`）。
- 检索来源：`docker compose logs worker --since 24h`。

### 2.2 Worker 容器日志（关键行）

```
worker-1  | 08:57:41.879 | INFO     | arq.worker:process_job:281 - ran task 'run_task' id=f3d1d6f6-9c12-407e-8eeb-62fbd802bd84 →
worker-1  | 09:04:30.563 | INFO     | pobi_agent.agents.executor:call_shell_agent:... - shell execution: `which avfs_mount || avfs_mount` → not found / 失败
worker-1  | 09:04:30.66x | ERROR    | pobi_agent.tools.avfs... - Error executing tool: AVFS workspace 'memory' is not mounted. Call avfs_mount first.
worker-1  | 09:04:37.xxx | (supervisor 仍继续推进，未因 memory 错误中断)
worker-1  | 09:16:44.xxx | INFO     | ... call_shell_agent ... - shell agent 目录枚举 (iteration 1→2→3)
worker-1  | 09:18:14.xxx | INFO     | ... - llm_input (第 73 条事件): ffuf 目录爆破命令输出
worker-1  | (此后无任何该任务的新事件产出)
```

> 注：`→` 表示任务被取出执行；正常情况下后续应出现 `←`（完成）/`!`（失败）/`↻`（取消）。本任务仅有 `→`，无后续状态返回。

### 2.3 数据库任务状态

```sql
SELECT id, status, created_at, updated_at FROM tasks
WHERE id='f3d1d6f6-9c12-407e-8eeb-62fbd802bd84';
-- id      | status  | created_at          | updated_at
-- f3d...  | running | 2026-08-16 08:57:xx | 2026-08-16 09:16:44
```

`updated_at` 停留在 `09:16:44`，距诊断时已逾 2.5 小时无更新，但 `status` 仍为 `running`。

### 2.4 数据库事件轨迹（倒序节选）

```sql
SELECT event_type, seq, created_at, left(payload::text, 220)
FROM task_events WHERE task_id='f3d1d6f6-...' ORDER BY seq DESC LIMIT 10;
```

| seq | event_type | created_at | payload 摘要 |
|-----|------------|------------|--------------|
| 73 | llm_input | 09:18:14 | ffuf 目录爆破命令输出 |
| 72 | llm_input | 09:16:44 | shell agent 迭代 2→3 目录枚举结果 |
| 71 | agent_message | 09:04:37 | supervisor 认为 memory 初始化成功 |
| 70 | tool_error | 09:04:30 | `AVFS workspace 'memory' is not mounted. Call avfs_mount first.` |
| 69 | tool_error | 09:04:30 | shell agent 执行 `avfs_mount` → command not found |
| 68 | agent_start | 09:04:30 | call_shell_agent: avfs_mount |

最后落库事件为 `09:18:14` 的 `llm_input`（ffuf 输出）。其后无任何新事件。

### 2.5 源码静态分析（根因 A 实证）

`pobi_agent/pobi_agent.py`：

```python
# DeadEndAgent.__init__ —— memory 以 agent_id 挂载
avfs.mount(
    workspace_root=self.memory_workspace_root,
    session_id=str(self.agent_id),   # ← key = agent_id
    workspace="memory",
)
```

`pobi_agent/agents/components/executor.py`：

```python
# _persist_agent_summary —— 写 memory 用 self.session_id（= task_id）
write_text(
    f"summaries/{agent_name}.md",
    content=...,
    session_id=self.session_id,   # ← key = task_id  ≠ agent_id
    workspace="memory",
    append=True,
)
```

`write_text`（`pobi_agent/tools/avfs/write.py`）内部调用 `avfs.resolve(..., session_id=session_id, workspace="memory")`，因 key 不匹配 → `avfs._require_state` 抛 `AVFS workspace 'memory' is not mounted`。

> 注：`AgentExecutor` 构造参数 `memory_session_id=str(self.agent_id)`（pobi_agent.py 第440行）仅用于 **读** memory（`_refresh_memory_context_for_task` / `MemoryAgent`），**写** memory 的 `_persist_agent_summary` 绕过了它，直接用 `self.session_id`，造成读写双 session 不一致。

### 2.6 源码静态分析（根因 B 实证）

`pobi_agent/sandbox/sandbox.py`：

```python
def execute_command(self, command, stream=True, timeout_seconds=None, shell_execution=True):
    ...
    if timeout_seconds:
        result = self._execute_with_timeout(...)      # 有超时
    elif stream:
        command_result = container.exec_run(          # ← 无超时路径
            cmd=shell_command, detach=False,
            tty=False, socket=True, stream=True,
        )
        result = {"streaming": True, "stream": command_result.output, ...}
    else:
        ...  # 非 stream 无超时路径
```

`stream=True` 且未传 `timeout_seconds` 时，走 `container.exec_run(..., stream=True)`，**无超时保护**。长命令（如 `ffuf` 爆破）挂起时无事件产出 → 前端卡死。

## 3. 根因结论

| 编号 | 根因 | 类型 | 影响 |
|------|------|------|------|
| A | `_persist_agent_summary` 写 memory 用 `task_id`，但挂载用 `agent_id`，命名空间不一致 | 逻辑缺陷（session key mismatch） | 每次子 Agent 完成持久化 summary 失败；错误被吞；supervisor 误判 memory 就绪 |
| B | `execute_command` streaming 路径无命令超时 | 健壮性问题（缺少超时） | 长/挂起命令永久 await，无事件产出，前端表现为卡死 |

二者关系：A 不直接卡死任务，但掩盖了 memory 初始化失败，使 supervisor 在无 memory 监管下放任 shell agent 执行无人监管的长时间命令，最终触发 B。

## 4. 修复方案

### 4.1 修复根因 A（必修）

**文件**：`pobi_agent/agents/components/executor.py`
**位置**：`_persist_agent_summary` 内 `write_text(..., workspace="memory")` 调用

将：

```python
write_text(
    f"summaries/{agent_name}.md",
    content=...,
    session_id=self.session_id,   # 错误：task_id
    workspace="memory",
    append=True,
)
```

改为：

```python
write_text(
    f"summaries/{agent_name}.md",
    content=...,
    session_id=self.memory_session_id,   # 正确：agent_id，与挂载一致
    workspace="memory",
    append=True,
)
```

**补充建议**：统一 AVFS memory 访问入口，所有 memory 读写均经 `self.memory_session_id`，消除双 session 隐患；并在 `_persist_agent_summary` 外层将 `avfs` 错误显式抛出让 supervisor 感知，而非静默吞掉。

### 4.2 修复根因 B（必修，卡死直接原因）

**文件**：`pobi_agent/sandbox/sandbox.py`
**位置**：`execute_command` streaming 路径（约第 329-344 行）

为 streaming 路径补充默认超时，或强制 shell agent 调用时传入 `timeout_seconds`：

方案 1（推荐，最小侵入）：在 `execute_command` 中为非 `timeout_seconds` 的 stream 路径也施加默认超时：

```python
default_timeout = timeout_seconds or settings.SANDBOX_COMMAND_TIMEOUT_SECONDS or 300
if stream:
    result = self._execute_with_timeout(container, shell_command, stream, default_timeout)
```

方案 2：在 supervisor 的 shell agent 调用层强制 `timeout_seconds`（如 300s），超时后返回 `timed_out` 让 supervisor 重新规划。

**补充建议**：在 supervisor 的 shell agent 调用层增加命令级超时与周期性心跳事件（如每 30s 产出一条 `heartbeat` 事件），避免"无事件产出"导致前端误判。

### 4.3 任务收敛（临时处置）

当前任务 `f3d1d6f6-...` 仍 `running` 且无新事件，建议先取消以释放 worker 槽位：

```bash
curl -X POST "http://<host>/api/v1/tasks/f3d1d6f6-9c12-407e-8eeb-62fbd802bd84/cancel"
```

依赖协作式取消 + ARQ 幽灵 job 清理使其收敛为 `cancelled`，前端状态恢复正常。

## 5. 验证计划

1. 应用 4.1、4.2 修复后，构造最小复现：子 Agent 完成 → 验证 `summaries/*.md` 成功写入 memory（无 `is not mounted` 错误）。
2. 执行一个会超时的长命令（如 `sleep 400`），验证其在 300s 超时后返回 `timed_out` 而非永久挂起。
3. 重跑任务 `f3d1d6f6...`，观察事件流持续产出、`updated_at` 推进、最终正常进入 `completed`/`cancelled`。
4. 回归验证 README 所述"报告 D：memory 命名空间统一修复"在当前镜像下确实生效（本任务事件曾出现 `is not mounted`，说明该修复未在运行环境真正落地，需同步回归）。

## 6. 涉及文件清单

| 文件 | 角色 |
|------|------|
| `pobi_agent/agents/components/executor.py` | 根因 A（`_persist_agent_summary` 写 memory 用错 session_id）；根因 B 调用点（shell agent） |
| `pobi_agent/pobi_agent.py` | memory 以 `agent_id` 挂载（第135-139行）；`AgentExecutor` 读 memory 用 `memory_session_id`（第440行） |
| `pobi_agent/tools/avfs/avfs.py` | `_require_state` 抛 `is not mounted` 错误 |
| `pobi_agent/tools/avfs/write.py` | `write_text` 按 `session_id` 解析 memory 路径 |
| `pobi_agent/sandbox/sandbox.py` | 根因 B（streaming 命令无超时，第329-344行） |
| `pobi_agent/tools/avfs/list.py` | `avfs_mount` 为 Python 工具，非沙箱命令（模型误调用的来源） |
