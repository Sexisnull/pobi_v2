# 上下文爆炸问题分析与顶层解决方案

> 本文记录「威胁建模（recon）阶段上下文无限膨胀」这一架构级问题的完整证据链与顶层治理方案。
> 性质：**架构级问题，非单点 Bug**。任何"只改某一处"的修复都无法根治。
> 范围：仅分析与方案设计，不含代码实现（按需求不新增代码）。

---

## 1. 问题定义

在 `threat_model` / exploit 阶段的 supervisor 多轮迭代中，注入 LLM 的上下文**无上限累积**，最终触顶模型的 context window，由 provider 直接报「上下文超限」错误。pydantic-ai 框架**不会自动总结或滚动裁剪消息列表**，因此一旦累积超过窗口，没有任何兜底，只有失败。

用户的原始判断（机制层面）完全正确：

- `execute_supervisor` 内部只 `supervisor.run(...)` 一次；
- 但 pydantic-ai 在一次 `run` 内允许多轮工具调用，supervisor 每调一个子 agent，工具结果就被追加进**同一次** `message_history`；
- 子 agent 返回给 supervisor 时若全量、不截断，则 `message_history` 跨所有轮次无限增长；
- 循环护栏（`usage_limits`）被关闭，循环只能依赖 supervisor 自己判定 root task 已解决；
- 最终只受模型 context window 约束，超出即报 provider 错误。

---

## 2. 上下文注入全景架构（三层模型）

要理解膨胀，必须把"注入 LLM 的上下文"拆成三层——它们由不同机制生成、由不同代码管理，膨胀源分布在**不同层**：

| 层 | 名称 | 生成机制 | 谁管 | 是否膨胀源 |
|---|---|---|---|---|
| **L1** | 静态层 | 每次进入阶段时拼固定模板（任务指令 + 极简 `target_context` + Agent 系统指令） | `pobi_agent.py` / Agent 定义 | 否（固定、小） |
| **L2** | 结构化重渲染层 | 每轮子 agent 干活前，从全量 `facts/executions` 库**重新渲染**拼装 | `StructuredContext.get_unified_context` | 是（success 无界 + 字符级兜底） |
| **L3** | 框架累积层 | supervisor 同一份 `message_history` 跨轮 `append` 所有子 agent 返回；无窗口、无摘要、无刹车 | pydantic-ai + `execute_supervisor` | **是（主膨胀源，跨轮无界 + 无 usage_limits）** |

> 关键认知：**L2 是"单轮工作上下文"，L3 是"跨轮对话历史"**。两者独立累积。先前两次评估都低估了 L3，而真正量级最大的膨胀在 L3。

---

## 3. 完整证据链（跨多处代码，非单点）

### 3.1 L3 框架累积层 —— 真正的根因（跨轮无界 + 无刹车）

**E1｜supervisor 单次 `run` 但内部多轮工具调用**
`execute_supervisor` 创建 router 后只 `run` 一次，多轮迭代发生在这一次 `run` 内部（由 supervisor 决策驱动工具调用）：
```594:600:pobi_agent/agents/components/executor.py
            # Create new router with agent tools and supervisor deps
            supervisor = SupervisorAgent(
                model=self.model,
                deps_type=SupervisorDeps,
                tools=[],
                available_agents=self.available_agents
            )
```

**E2｜所有子 agent 工具调用共享同一份 `message_history`（跨轮累积的载体）**
六处子 agent 调用全部传入 `ctx.deps.message_history`——pydantic-ai 会把每轮工具结果追加进这份共享列表，且入参即出参（同对象 mutate），因此历史跨所有轮次、所有子 agent 共享累积：
```751:754:pobi_agent/agents/components/executor.py
                result = await ctx.deps.authenticator_agent.run(
                    f"{memory_prefix}{prompt}",
                    deps=ctx.deps.requester_deps,
                    message_history=ctx.deps.message_history,
```
```797:800:pobi_agent/agents/components/executor.py
                result = await ctx.deps.requester_agent.run(
                    f"{memory_prefix}{footprint_prefix}{prompt}",
                    deps=ctx.deps.requester_deps,
                    message_history=ctx.deps.message_history,
```
```829:832:pobi_agent/agents/components/executor.py
                result = await ctx.deps.shell_agent.run(
                    f"{memory_prefix}{prompt}",
                    deps=ctx.deps.shell_deps,
                    message_history=ctx.deps.message_history,
```
（另见 `webapp_analyzer_agent` `859-862`、`memory_agent` `877-880`/`906-911`，共 6 处。）

**E3｜单块限长已修，但只缩小"砖"，不限制"墙"**
你已把子 agent 返回给 supervisor 的内容从全量改为限长（这是有效改进，消去了"单块全量"问题）：
```180:195:pobi_agent/agents/components/executor.py
def _format_tool_result_for_supervisor(agent_name: str, output: Any) -> str:
    """Render tool/agent output in a stable format that preserves key details for downstream reasoning.

    切片 3 P2：detailed_summary/proofs/thoughts 限长，避免单轮子 agent 输出全量注入 supervisor 消息列表。
    """
    if isinstance(output, AgentOutput):
        summary = (output.detailed_summary or "None")[:1500]
        proofs = (output.proofs or "None")[:500]
        thoughts = (output.thoughts or "None")[:300]
```
但限长只约束**单块**，跨轮 `append` 仍无上限——砖变小了，墙仍无界。

**E4｜循环刹车（`usage_limits`）被关闭**
`threat_model` 路径与另外 5 处调用点全部传 `request_limit=None, tool_calls_limit=None`；`execute_supervisor` 默认值也是 `None`。即 supervisor 调子 agent 无次数上限、无 token 上限：
```596:597:pobi_agent/pobi_agent.py
            usage=RunUsage(),
            usage_limits=UsageLimits(request_limit=None, tool_calls_limit=None),
```
```482:483:pobi_agent/agents/components/executor.py
        usage: RunUsage = RunUsage(),
        usage_limits: UsageLimits = UsageLimits(request_limit=None, tool_calls_limit=None),
```
（其余调用点：`pobi_agent.py:350/1118`、`executor.py:463` 等，共 6 处同态。）

**E5｜pydantic-ai 不自动裁剪消息列表（框架行为结论）**
`message_history` 由框架管理，无内置的滚动窗口/摘要/截断；超出 context window 即由 provider 报错。本仓库无任何代码对此列表做窗口化或压缩（检索全仓无 `message_history` 截断/摘要逻辑）。

> **E1+E2+E4+E5 串联**：单次 `run` 内多轮工具调用 → 结果追加进共享 `message_history` → 无 `usage_limits` 次数/ token 刹车 → 框架不自动裁剪 → 循环直到触顶 context window 才失败。这是 L3 膨胀的完整因果链。

---

### 3.2 L2 结构化重渲染层 —— 已部分治理，但仍有真实漏洞

**E6｜`get_unified_context` 是统一格式化入口，且额外挂载 RECON 块**
它是所有子 agent 共用的"单源格式化函数"（`StructuredContext.get_unified_context` + RECON 分层注入块）：
```1125:1154:pobi_agent/context/context_engine.py
    def get_unified_context(self, max_tokens: int = 6000) -> str:
        """Get UNIFIED context for ALL agents.
        ...
        """
        ...
        base = self.structured.get_unified_context(max_tokens=max_tokens)
        # 挂载 RECON 本地库 L0/L1/L2 分层注入块（已有历史侦察资产复用）。
        recon_block = self._build_recon_index_block()
        if recon_block:
            return f"{recon_block}\n\n{base}"
        return base
```
子 agent 每轮从全量 `facts/executions` 库**重新渲染**（印证第 ⑤ 点认知修正：反馈是"落库→下一轮重渲染注入"，非直连 supervisor）。

**E7｜SECTION 3 的 `success` 分支无上限（L2 内的真实无界源）**
安全测试场景下对同一 endpoint 反复微调 payload 命中同一 technique，会产生大量 `success` 记录，渲染时**全保留**：
```697:702:pobi_agent/context/context_engine.py
                if successes:
                    lines.append("✓ WORKED:")
                    for ex in successes:
                        lines.append(f"  - {ex.technique}")
                        if ex.key_finding:
                            lines.append(f"    → {ex.key_finding[:100]}")
```
对照：`failures` 已限 5 条（`704-710`），而 `successes` 无任何上限——facts 库里 success 可无限多，渲染时全量输出。

**E8｜`max_tokens` 是字符数而非 token 数，且兜底粒度粗**
```804:827:pobi_agent/context/context_engine.py
        # max_tokens 总预算：按优先级从低到高删除 section，直到 <= max_tokens
        result = "\n\n".join(sections)
        if len(result) > max_tokens:
            ...
            for marker in deprioritized:
                for i, s in enumerate(sections):
                    if s.startswith(marker):
                        sections.pop(i)
                        break
                result = "\n\n".join(sections)
                if len(result) <= max_tokens:
                    break
            # 最终兜底：硬截断（预留截断提示空间，确保总长度 <= max_tokens）
            if len(result) > max_tokens:
                trunc_note = "\n... [truncated to max_tokens]"
                result = result[:max_tokens - len(trunc_note)] + trunc_note
```
两个缺陷：① `len(result)` 是**字符数**，与参数名 `max_tokens` 语义错位（中英文混排下 6000 字符 ≈ 2000~3000 token）；② 优先级兜底"整段删 SECTION"（如整段删 `COMPLETE TEST HISTORY`），粒度粗，可能把最有价值的"哪些 technique 有效"整体丢弃，而非行级收敛。

**E9｜RECON 块自带 token 预算（对照：非膨胀源，已受控）**
`build_index_view` 内部 L0/L1/L2 分层、确定性 token 预算裁剪，注入不超预算：
```60:62:pobi_agent/recon/store.py
# 分层索引 token 预算（设计文档 L1<500、L2<1500）。
L1_TOKEN_BUDGET = 500
L2_TOKEN_BUDGET = 1500
```
```752:763:pobi_agent/recon/store.py
        # ---- L1: 相关端点/技术栈（≤500 tokens）----
        l1_lines = ["## L1 端点与技术栈 (recon)"]
        ...
        l1_block = self._fit_budget("\n".join(l1_lines), L1_TOKEN_BUDGET)
```
→ 说明前置侦查落库注入（`_build_recon_index_block`）**不是**无约束通道，无需额外治理。

---

### 3.3 回退/退化通道 —— 已验证非主膨胀源（但需显式排除，避免误判）

**E10｜`workflow_context` 仅结构化为空才回退进 prompt**
```1084:1094:pobi_agent/context/context_engine.py
        structured_context = self.structured.get_executor_context(max_tokens=max_tokens)
        # If structured context is empty, fall back to workflow_context (backward compat)
        if not structured_context or len(structured_context) < 50:
            tokens = await self.maybe_summarize_context()
            if tokens > 10000:
                logger.debug("Using workflow_context (%d tokens)", tokens)
            return self.workflow_context
        return structured_context
```
所有 agent 主通道已走 `StructuredContext`，`workflow_context` 仅作空值 fallback + 调试日志，不进入主 prompt。

**E11｜`workflow_context` 自身有环形截断 + 高阈值摘要**
```1171:1173:pobi_agent/context/context_engine.py
    async def maybe_summarize_context(
        self,
        token_threshold: int = 200_000,
```
```1228:1233:pobi_agent/context/context_engine.py
        self.workflow_context += f"\n{response}\n"
        # 环形截断：超过阈值时从开头截断，保留最近内容（完整历史已落盘 context.txt）
        if len(self.workflow_context) > self._max_workflow_chars:
            self.workflow_context = self.workflow_context[-self._max_workflow_chars:]
```
→ 即便 fallback，也有 200k 阈值 + 环形截断，且触发后调 ReporterAgent 摘要；风险远低于 L3。

**E12｜executor 灌入的"全文 fact"不被渲染**
`_add_agent_output_to_context` 把 `detailed_summary/proofs` 以 `agent_result`/`agent_proofs` 类别全量落 `facts`（`executor.py:619-642`，注释仍写 `NO TRUNCATION`），但 `get_unified_context` 的 facts 过滤列表不含这两类：
```714:716:pobi_agent/context/context_engine.py
        findings = [f for f in self.facts.values()
                    if f.category in ("finding", "technology", "attack_vector", "feature")]
```
→ 这些"全文 fact"只落 SQLite，不渲染进主 prompt，对实际注入 token 量**当前无影响**。先前评估曾误判其为膨胀源，此处显式排除。

---

## 4. 证据链路串联（因果总图）

```
[前置侦查/子 agent 执行]
        │ 落库
        ▼
facts / executions / recon_store（无限增长，无约束）
        │ 每轮重渲染（L2）
        ▼
StructuredContext.get_unified_context  ── E6/E7/E8（success 无界 + 字符级兜底）
        │ 拼装为子 agent 单轮工作上下文
        ▼
supervisor message_history（L3）  ── E1/E2（单次 run 多轮 + 共享列表 append）
        ▲                              E4（usage_limits=None 无刹车）
        │ 子 agent 返回限长结果追加入列表  ── E3（单块限长已修，但跨轮无界）
        │                              E5（框架不自动裁剪）
        └──────────────────────────── 循环直到触顶 context window → provider 报超限错误
```

膨胀量级估算（示意，非实测）：recon 循环 30 轮 × 3 子 agent × 单块 ≤2300 字 ≈ 20 万字符 ≈ 6~7 万 token，全堆在 `message_history`，且每个子 agent 调用都背着它——远超 L2 的 `get_unified_context`（≤6000 字符）数个数量级。

---

## 5. 顶层解决方案（全局视角，分层治理）

> 设计原则：**每一层独立设上限，且装配期统一结算**。不依赖任何单一修复点；任一层的缺失都会让整体再次失守。

### M-L3a ｜ 跨轮历史窗口化 / 摘要（治主膨胀源，须架构驱动）
> ⚠️ 实现约束（已核实，见 §8）：pydantic-ai 1.35 **没有** in-run 裁剪 hook，`message_history` 由框架 run state 拥有，单次 `run()` 内无法裁剪。因此"窗口/压缩"**只能在 `run()` 之间的边界做**：取 `result.all_messages()` → 裁剪 → 作为 `message_history` 重传下一次 `run()`。这要求把当前"supervisor 内部单次 `run()` 多轮工具调用"的循环，**改为由我们自己的驱动循环逐轮调用 `supervisor.run()`**（详见 §8 路径 A）。

对 supervisor 的 `message_history` 做滚动窗口管理，而非无界 append：
- 方案 A（窗口）：保留最近 K 轮工具结果 + 首轮系统上下文，早期轮次移出窗口；
- 方案 B（压缩）：每 K 轮把窗口外历史用轻量 LLM/规则压缩为一段"阶段摘要"重新注入；
- 二者均须保证**被移出/压缩的内容仍可从落库（facts / context.txt）回退检索**，不静默丢信息。

### M-L3b ｜ `usage_limits` 硬刹车（循环安全网，原生可用）
给 `threat_model` / exploit 路径设有限 `usage_limits`（`request_limit` 或 `total_tokens_limit` 上限），作为循环终止的安全网：
- 这是 **pydantic-ai 原生刹车**（`usage.py:418` 定义 `request_limit`/`tool_calls_limit`/`total_tokens_limit`，`:494-559` 在每次请求前强制校验并抛 `UsageLimitExceeded`）；
- **本项目显式传 `request_limit=None` 把它关掉了**（`pobi_agent.py:597` 等 6 处），恢复有限值即可获得框架级硬上限，零自研代码；
- 天然限制 `message_history` 的**总量上限**（轮次×单块有界）；防止 supervisor 因决策偏执无限转圈；
- 注意：`request_limit` 默认本就是 `50`（`usage.py:429`），本项目是主动置 `None` 才放开；恢复时应基于历史任务轮次分布设定，避免误杀正常长任务。

### M-L2a ｜ `success` 分支去重 / 预算上限（L2 内真实无界源）
`context_engine.py:697-702` 的 `successes` 全保留改为：
- **去重（推荐，最贴合业务）**：按 `(endpoint, technique)` 去重，每条 technique 仅列一次——模型只需知"该 technique 对该 endpoint 有效"，无需看 N 次重复成功；
- 或按 token 预算逐行登记，耗尽追 `OMIT_HINT`。

### M-L2b ｜ 字符预算 → token 预算（精度修正）
`get_unified_context` 的 `len(result) > max_tokens`（字符）改为 token 估算（tiktoken / `num_tokens_from_string`，仓库 `recon/store.py` 已用 `num_tokens_from_string` 可直接复用），使 `max_tokens` 语义名副其实；并将"整段删 SECTION"兜底改为**行级收敛**，避免粗粒度丢高价值信息。

### M-统一装配预算（顶层闭环）
在装配期引入**单一预算令牌**（原 plan 的 `ContextBudget` 思路：真实 token 计数 + 全局优先级结算 + 行级 `OMIT_HINT` 引导 `recon_lookup` 回退检索），统一结算 L1/L2/RECON 各 block 与 L3 窗口总量，保证"注入 LLM 的总 token 数 ≡ ≤ 预算"恒成立。RECON 块（`build_index_view` 已有预算）与 L2 应纳入同一结算器，避免各 block 预算相加后整体仍超窗。

### 治理闭环校验（验收）
- 注入总量硬上限：存在 `usage_limits` + L3 窗口 + L2 token 预算三重上限，单点失效不导致整体爆；
- 信息不静默丢失：被裁剪/压缩内容均可从落库回退；
- 单块限长（E3 已修）保留，作为 L3 窗口内的第二道防线。

---

## 6. 当前已修复 vs 待修复矩阵

| 治理点 | 位置 | 状态 |
|---|---|---|
| E3 单块限长（子 agent→supervisor） | `executor.py:180-195` | ✅ 已修复 |
| E7 success 分支无界 | `context_engine.py:697-702` | ❌ 待修复（M-L2a） |
| E8 字符级 budget / 粗粒度兜底 | `context_engine.py:804-827` | ⚠️ 部分（M-L2b） |
| E4 usage_limits 刹车 | `pobi_agent.py:597` 等 6 处 | ❌ 待修复（M-L3b） |
| E2/E5 message_history 跨轮累积无界 | `executor.py:751-911` | ❌ 待修复（M-L3a，主膨胀源） |
| E9 RECON 块预算 | `store.py:60-805` | ✅ 已受控（无需动） |
| E10/E11/E12 退化通道 | `context_engine.py` | ✅ 已验证非主膨胀源 |

**结论**：当前已缓解"单块全量注入"（E3）与"中小规模 L2 渲染"，但**未根本解决**威胁建模循环阶段爆炸——根因在 L3（`message_history` 跨轮无界 + 无 `usage_limits` 刹车），该层完全未被治理。必须按 M-L3a + M-L3b 补齐，配合 M-L2a/M-L2b 方能从全局根治。

> 各治理点的**实现可行性/机制**见 §8：M-L3a 须架构驱动循环（路径 A，非 hook），M-L3b 为 pydantic-ai 原生 `UsageLimits` 配置（零自研代码）。

---

## 7. 风险与验证建议

- **风险**：M-L3a 窗口化若压缩不当，可能丢失早期关键决策上下文，导致 supervisor 重复探索；须保证落库可回退。
- **验证**：构造长循环 recon 任务，观测 `message_history` 长度随轮次是否收敛；注入总 token 是否恒 ≤ 预算；超长任务是否以 `usage_limits` 安全终止而非 provider 报错。
- **不引入破坏性变更**：M-L3b 的 `usage_limits` 上限需基于历史任务轮次分布设定，避免误杀正常长任务。

---

## 8. 实现路径探索：message_history 可裁剪性核实（关键修订）

> 用户指出：pydantic-ai 的 `message_history` 在 `run()` 内部管理，外部无法直接裁剪，需探索 hook（`message_provider` / `result_writer` / `on_message`）或改架构。本节基于 **pydantic-ai 1.35 源码**（`pyproject.toml` 约束 `>=1.35.0`）逐项核实，结论与"靠 hook 裁剪"的直觉不同。

### 8.1 核实结论：本版本无 in-run 裁剪 hook

| 用户预期的 hook | 1.35 是否存在 | 证据 |
|---|---|---|
| `message_provider` | ❌ 不存在 | 全仓 `rg` 无匹配 |
| `result_writer` | ❌ 不存在 | 全仓 `rg` 无匹配 |
| `on_message` | ❌ 不存在 | 全仓 `rg` 无匹配 |
| `Model.Hooks.on_messages` / `on_model_response` | ❌ 不存在 | `models/` 下无 `class Hooks` / `on_messages`（仅 `on_model_request_error` 能力 hook，用于重试，非裁剪） |
| `history_processors` 参数 | ❌ 未接线 | `HistoryProcessor` 类型在 `_history_processor.py` 定义并被 `_agent_graph.py` 导出，但**无任何 run/agent 参数引用它**（全仓 `rg "history_processors"` 仅类型定义本身） |

→ **结论**：在 1.35 没法用一个 hook 在单次 `run()` 的工具调用循环里裁剪 `message_history`。你说的"message_history 在 run() 内部管理，外部无法直接裁剪"**完全正确**。

### 8.2 框架实际行为（为何外部不可裁剪）

- `RunState.message_history` 由框架在 run 内部持有（`_agent_graph.py:296` `message_history: list[ModelMessage] = field(default_factory=list)`）；传入的 `message_history` 仅作初始化副本，run 期间由框架独占写入。
- 每次向模型发请求前，框架调用 `_clean_message_history(ctx.state.message_history)` 并 **整体重新赋值** `ctx.state.message_history`（`_agent_graph.py:530-532`）。该函数只清理空/冗余 part，**不做窗口化或摘要**——这是框架内唯一的"裁剪点"，且不可外部配置。
- 模型请求节点（`_agent_graph.py:1087`）+ 工具执行节点构成内部循环：模型返回工具调用 → 执行工具 → 追加 tool-return → 再次请求模型。整个循环发生在**一次 `run()`** 内，外部无介入点。

### 8.3 框架真实可用的两个杠杆

1. **`UsageLimits`（原生硬刹车）** — `usage.py:418`：
   ```418:437:.venv/lib/python3.12/site-packages/pydantic_ai/usage.py
   class UsageLimits:
       request_limit: int | None = 50
       tool_calls_limit: int | None = None
       total_tokens_limit: int | None = None
   ```
   每次请求前校验并抛 `UsageLimitExceeded`（`:494-559`）。本项目显式 `UsageLimits(request_limit=None, ...)` 把它关掉。**恢复有限值 = 零自研代码获得硬上限。** 这是 M-L3b 的落地基础。

2. **`run()` 边界重传（唯一窗口化入口）** — `RunResult.all_messages()` 返回完整历史（`run.py:168/651`），下一轮 `run(message_history=裁剪后列表)` 重传。裁剪只能在**两次 `run()` 之间**做，不能在单次 run 内做。

### 8.4 三种实现路径对比

**路径 A（推荐，架构驱动循环）— M-L3a 的真正落地方式 ✅ 已选并实现**
把当前"supervisor 单次 `run()` 内部多轮调子 agent"改为：**我们自己的驱动循环**逐轮调用 `supervisor.run()`，每轮只让 supervisor 产出"下一步决策（调哪个子 agent + 参数）"而非内部递归调工具；驱动层执行子 agent、收集结果、**对 `all_messages()` 做窗口/压缩后**作为 `message_history` 重传给下一轮 `supervisor.run()`。
- 优点：在**我们拥有**的边界上裁剪，版本无关、可控、可回退检索；同时让 M-L2a/L2b 的 token 预算在每轮渲染时生效。
- 代价：需重构 supervisor 从"router（工具调用子 agent）"变为"决策器（返回结构化下一步）"，子 agent 调用从 supervisor 工具改为驱动层调用。属中量级架构改动，但直击根因。

> **实现落地（当前会话）**：
> - `supervisor.instructions.jinja2` 由 router 改写为 **decider**：supervisor 不再持有子 agent 工具，改为输出结构化 `SupervisorDecision{action: call_agent|complete, agent, prompt, task_achieved, confidence_score, detailed_summary, proofs}`。
> - `executor.execute_supervisor` 新增**驱动循环**：每轮 `window_messages(history)` 裁剪 → `supervisor.run(message_history=窗口化, usage_limits=有界)` → 解析决策 → `action==call_agent` 时由 `_run_sub_agent` 直调子 agent（`_ToolCtx` 垫片复用既有 `call_*`，每次独立 `message_history` 不回灌），`complete` 时产出 `ResultEvent`。
> - 裁剪边界：`result.raw_messages`（等价 `all_messages()`）取回 → 剥离 system 后窗口化重传；存储列表本身每轮经 `window_messages` 重赋，避免跨轮无界累积（L3 主膨胀源根治）。
> - 跨 ADaPT 轮持久：`architecture.py` 外层 `while` 维护 `supervisor_history: list[dict]`，同 node 迭代间传递并在换节点时重建。
> - M-L3b 刹车：`_DEFAULT_SUPERVISOR_LOOP_CONFIG`（request_limit=40 / history_max_messages=24 / history_max_tokens=6000）经 `SupervisorLoopConfig.usage_limits` 透传 `UsageLimits`，框架层 `FallbackAgentResult` 触顶即终止循环。
> - 验证：`tests/test_supervisor_loop.py`（驱动多轮委派→complete 终止、history 有界 ≤24、子 agent 被委派、UsageLimits 触顶即停）8 例全绿；落库闭包（`_add_agent_output_to_context`/`_persist_agent_summary`/`_register_auth_facts_in_context`）零改动，findings 无回归。
- 与现有代码的契合点：子 agent 现已是独立 `Agent`（`executor.py:751-911` 各自 `.run`），改为驱动层调用是自然演进，不破坏落库/反馈闭环（§2 第⑤点）。

**路径 B（工具内裁剪，脆弱，不推荐为主）**
保留子 agent 作为 supervisor 工具，在工具函数内（子 agent `.run` 返回后）直接改写共享的 `ctx.deps.message_history`。
- 风险：`ctx.deps.message_history` 与框架 `ctx.state.message_history` 的别名关系在不同版本/节点间不稳定（`_agent_graph.py:532` 每轮整体重赋值 state），工具内改写可能被框架覆盖或漏裁，**不可靠**，且依赖未公开的内部契约。仅作应急过渡，不作为根治方案。

**路径 C（仅 `UsageLimits`，安全网）**
仅恢复有限 `request_limit`/`total_tokens_limit`，不做窗口化。
- 优点：零架构改动、立即可用（M-L3b）。
- 局限：只给"总量硬上限 + 循环终止"，**不降低单次调用的上下文体积**——长任务仍会在触顶前把 `message_history` 撑到上限，且 `request_limit` 上限即"最多轮次"，语义是"限轮"非"限 token 体积"。须与 A 配合。

### 8.5 升级路径

若未来 pydantic-ai 将 `HistoryProcessor`（`_history_processor.py` 已定义类型）接线为 `run()`/`Agent` 的 `history_processors` 参数，则可零架构改动地实现"每次模型请求前自动窗口化"，届时路径 A 可降级为该 hook 实现。当前版本不可依赖。

### 8.6 落地优先级（修正后）

| 顺序 | 动作 | 类型 | 根因覆盖 | 状态 |
|---|---|---|---|---|
| 1 | 恢复 `UsageLimits` 有限值（驱动循环统一透传） | 配置（零自研） | M-L3b 刹车，立即止血 | ✅ 已落地（路径 A） |
| 2 | 改架构：驱动层逐轮 `supervisor.run()` + `raw_messages` 窗口/压缩重传 | 架构重构 | M-L3a 主膨胀源根治 | ✅ 已落地（路径 A） |
| 3 | `success` 去重 / token 预算 | L2 渲染 | M-L2a | ⏳ 见 M1 计划（正交，未并入路径 A） |
| 4 | `len()` → token 估算 | L2 精度 | M-L2b | ⏳ 见 M1 计划（正交，未并入路径 A） |
| 5 | 统一装配预算（ContextBudget 思路） | 顶层闭环 | 全局结算 | ⏳ 待规划 |
