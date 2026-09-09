# 人工干预机制审计（任务运行期指令注入）

> 审计日期：2026-09-09
> 审计范围：任务详情页对话框 → 后端指令接口 → 指令通道 → 运行中的 Agent 消费链路
> 结论先行：**当前人工干预机制无法真正生效，存在两处致命缺陷 + 一处结构性盲区。**

---

## 一、预期设计（应有行为）

用户通过任务详情页对话框，在任务 `running` 期间向主控 Agent 追加指令，Agent 应在下一个 supervisor 迭代中自然纳入该指令（协作式检查点轮询）。

设计文档声称（见 `pobi_v2/engine/instruction_channel.py` 1-13 行）：
- 前端 `POST /api/v1/tasks/{id}/instructions` 写入 per-task pending 队列；
- Worker 在 `run_exploitation` 的协作式检查点轮询并消费；
- 指令注入 Supervisor 上下文。

## 二、实际调用链（代码证据）

### 写入侧（正确）

```
TaskConsole.jsx  →  POST /api/v1/tasks/{id}/instructions
   └─ instruction.py:40  →  queue_instruction(str(task.id), data.instruction, ...)
        └─ instruction_channel.py:119  →  _MemoryInstructionStore.push(task_id, item)
                                          key = str(task.id)   ✅ task_id 维度
```

### 消费侧（错误）

```
executor.py:run_task
   └─ pobi_agent.py:786  run_exploitation(threat_model, task)
        └─ pobi_agent.py:841  async for event in self.adapt_agent.run(task=task, context=exploit_context):
             └─ pobi_agent.py:852  pending = await drain_instructions(self.agent_id)   ❌ agent_id 维度
                  └─ instruction_channel.py:137  drain_instructions(task_id)
                                                 key = str(agent_id)  ≠ str(task.id)
```

### Supervisor 上下文构建（错误放大）

```
architecture.py:538  ADaPTAgent.run(task, context=exploit_context)
   └─ architecture.py:247-259  _solve 每轮重建上下文：
        tasks_context      = self.context.get_tasks(...)
        unified_context    = self.context.get_unified_context(...)
        agent_context      = f"{unified_context}\n\n{history_block}\n\n{tasks_context}"
        executor_stream    = self.executor.execute_supervisor(task_node=node, agent_context=agent_context, ...)
```

---

## 三、问题清单（含证据代码）

### 问题 1【致命】指令通道 key 不匹配：写入用 task_id，读取用 agent_id

**写入端** `pobi_v2/routers/instruction.py:40-44`：
```python
await queue_instruction(
    str(task.id),                       # key = task_id（UUID）
    data.instruction,
    meta={"created_by": user.email, "tenant_id": str(user.tenant_id)},
)
```

**存储端** `pobi_v2/engine/instruction_channel.py:36-46`：
```python
self._queues: dict[str, list[PendingInstruction]] = {}
def push(self, task_id, item):
    self._queues.setdefault(str(task_id), []).append(item)   # 按 task_id 索引
```

**消费端** `pobi_agent/pobi_agent.py:852`：
```python
pending = await drain_instructions(self.agent_id)            # 用 agent_id 索引
```

**事实核对**：
- `pobi_agent.py:111` `self.session_id = session_id`
- `pobi_agent.py:132` `self.agent_id = self.local_agent_id`
- `pobi_v2/engine/scan_workflow.py:160` `DeadEndAgent(session_id=task_id, ...)` → `session_id == task_id`
- 因此 `self.agent_id`（本地生成的 UUID）≠ `self.session_id`（= task_id）≠ 指令写入的 key。

**后果**：用户指令写入 `queues[str(task.id)]`，Worker 从 `queues[str(agent_id)]` 读取 → 恒成立空队列。**对话框发得出、后端存得下，但运行中 Agent 永远读不到，干预形同虚设。**

---

### 问题 2【致命】注入目标变量未被任何下游消费

`pobi_agent.py:855` 把指令拼接进了 `exploit_context` 局部变量：
```python
exploit_context = exploit_context + inject
logger.info("已注入运行指令: %s", item.instruction[:80])
```

但 `exploit_context` 作为 `adapt_agent.run(task=task, context=exploit_context)` 的 `context` 参数传入 `ADaPTAgent.run`，而该 `context` 仅在 `architecture.py:570` 用于**首轮 planner.expand**：
```python
subtasks, website_info, exploit_info = await self.planner.expand(
    root,
    context=context if context else "",   # 仅规划阶段一次性使用
    ...
)
```

而每个 supervisor 迭代实际使用的上下文在 `_solve` 内每轮**从 ContextEngine 动态重建**（`architecture.py:247-259`），完全不引用 `exploit_context` / `context` 变量。`run()` 入参 `context` 在规划结束后即被丢弃。

**后果**：即便把问题 1 的 key 改成 task_id，第 855 行的 `exploit_context = exploit_context + inject` 也只是修改了一个**已没有任何读取方**的局部变量。注入的指令**永远不会进入 supervisor 视野**。

---

### 问题 3【结构性盲区】威胁建模阶段完全无法干预

`run_exploitation` 的指令检查点只位于 `async for event in self.adapt_agent.run(...)`（`pobi_agent.py:841-858`）——即**仅在利用阶段**轮询。

威胁建模阶段 `threat_model()`（`pobi_agent.py:514` 起）与 `run_exploitation` 是 `scan_workflow.py` 中串行调用的两个独立协程（第 177、253 行）。用户若在威胁建模期间发指令，要等到利用阶段 `adapt_agent.run` 启动后才会被处理——且无任何提示告知用户指令被延迟。

**后果**：长威胁建模任务下，人工干预存在不可预期的"冷启动延迟"，违背"实时干预"的设计意图。

---

### 问题 4【弱可观测性】消费异常被静默吞掉

`pobi_agent.py:857-858`：
```python
except Exception as _exc:  # noqa: BLE001
    logger.debug("指令检查点读取失败，跳过: %s", _exc)
```
配合问题 1，调试时表面"无报错"，实则干预从未发生，排障难度高。

---

## 四、调用链全景图

```
[前端] TaskConsole.jsx
   │  POST /instructions
   ▼
[路由] instruction.py  post_instruction
   │  status==running 校验
   │  queue_instruction(str(task.id), ...)        ── key: task_id
   ▼
[通道] instruction_channel.py  _MemoryInstructionStore.push
   │  queues[str(task.id)] = [item]
   │
   │  ╔═══════════════ 关键断裂点 ═══════════════╗
   │  ║  此处队列按 task_id 写入，但读取按 agent_id  ║
   │  ╚════════════════════════════════════════════╝
   ▼
[Worker] executor.py → run_task
   └─ DeadEndAgent.run_task → pobi_agent.run_exploitation
        ├─ threat_model()                      ← 无检查点（盲区）
        └─ adapt_agent.run(task, context=exploit_context)
             └─ async for event:
                  pending = drain_instructions(self.agent_id)  ← key 不匹配，恒为空
                  exploit_context = exploit_context + inject   ← 变量无下游消费
                  → supervisor 每轮用 ContextEngine 动态重建的 agent_context（不含指令）
```

---

## 五、修复方案

### 修复 A（必做，解决致命问题 1+2）：统一 key 并注入 ContextEngine

**1) 修正 key 一致**：消费端改用 `self.session_id`（== task_id）。
```python
# pobi_agent.py:852
pending = await drain_instructions(self.session_id)   # 原 self.agent_id
```

**2) 真正注入可被 supervisor 读取的上下文**：不能再改 `exploit_context` 局部变量，应写入 `ContextEngine`。推荐在检查点把指令作为 `add_discovered_fact` / 专门的 operator instruction 注入，使每轮 `_solve` 重建的 `unified_context` 包含它：
```python
from pobi_agent.context.context_engine import OperatorInstruction  # 如有，否则用 add_discovered_fact
for item in pending:
    self.context.add_operator_instruction(item.instruction)  # 需在 ContextEngine 暴露该方法
    logger.info("已注入运行指令: %s", item.instruction[:80])
```
并在 `architecture.py:247-259` 的 `unified_context` 构建中纳入 operator instructions（如 `_get_operator_instructions()`），使 supervisor 下一轮必然看到。

**3) 同时修正 peek 展示**：前端 `/live` 用 `peek_instructions` 同样需传 `session_id` 而非 agent_id（若存在类似误用，统一改用 task_id）。

### 修复 B（必做，解决盲区问题 3）：威胁建模阶段也加检查点

在 `threat_model()` 的主循环/`async for event` 内增加与 `run_exploitation` 同构的 drain 检查点，并将指令注入威胁建模所用的上下文构造入口。

### 修复 C（建议，解决可观测性问题 4）：异常分级上报

`pobi_agent.py:857-858` 的 `except` 改为 `logger.warning` 并标记任务级 warning；若连续 N 次读取失败，通过事件总线向前端推送提示，避免"无痕失效"。

### 修复 D（建议）：加调用链测试固化不回归

新增集成测试：启动一个 mock Agent → 任务 running 时 `POST /instructions` → 断言 supervisor 下一轮上下文包含该指令文本。覆盖 task_id/agent_id 一致性与 ContextEngine 注入两条路径，防止再次脱节。

---

## 六、最小可验证验证步骤

1. 修复 A-1 + A-2 后，启动任务，运行至利用阶段。
2. 任务详情页对话框发送指令："只测试 SQL 注入，忽略其他"。
3. 预期：`logger.info("已注入运行指令...")` 出现，且后续 supervisor 日志（architecture.py:249-253 的 `[ADAPT] 上下文快照`）的 `unified_context` 含该指令文本。
4. 当前（未修复）下：日志无"已注入"且 supervisor 上下文永不含指令 → 已实测可复现（代码静态确认：key 不匹配 + 变量无消费方）。

---

## 七、影响评估

- **功能影响**：人为修正机制 100% 失效，对话框仅作"已接受"假象，无任何实际干预效果。
- **安全影响**：无（指令仅写入、未越权执行），但用户误以为能干预，可能在误操作后无止损能力。
- **优先级**：P0（核心交互功能完全不可用）。
