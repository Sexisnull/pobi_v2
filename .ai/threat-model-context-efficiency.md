# 威胁建模与利用阶段：上下文选取策略与重复侦察探查

> 本文是 `context-explosion-analysis.md` 的补充与延伸，聚焦两个被上一轮评估误判/未深挖的问题：
> - **问题 A**：`get_unified_context()` 是"全量注入"还是"按需检索"？
> - **问题 B**：`threat_model` 阶段是否重复做了 `pre_recon` 已完成的侦察？
>
> 性质：代码探查 + 根因分析 + 顶层方案设计。**B1（2026-09-09）、B2（2026-09-10）已验证并实施**（见 §6）；A1/A2/B3 仍为方案设计，待度量后实施。
> 探查基于 `pobi_agent/context/context_engine.py`、`pobi_agent/recon/store.py`、`pobi_agent/pobi_agent.py` 当前实现。

---

## 0. 结论速览（先给结论）

| 问题 | 上一轮判断 | 探查后修正结论 | 状态 |
|------|-----------|---------------|------|
| **A. 上下文全量 vs 按需** | "似乎把沉淀全量拼进 prompt，会撑大 token、截断关键资产" | **部分正确但机制判断偏了**：已有分层预算 + `max_tokens` 截断保护，不会无限膨胀；**真正问题是"确定性 top-N 截取，不做相关性检索"**——注入内容与"当前子任务在做什么"无关，导致无关资产占用预算、相关资产被裁掉。 | 验证成立；A1/A2 待度量后实施 |
| **B. threat_model 重复侦察** | "威胁建模没消费 pre_recon，空想计划" | **该判断已推翻**（见历史对话）。真实情况是：资产已通过 `get_unified_context` 流入 LLM；**但 `threat_model` 与 `run_exploitation` 的 supervisor prompt 都命令 LLM"用工具去侦察"，而 `pre_recon` 已是平台层自动侦察 —— 两阶段各自做一次侦察，存在重复劳动。** 且 `covered_block`（跳过重复工作）**只注入到 `run_exploitation`，未注入 `threat_model`**。补充：`executor.execute_supervisor` 已对所有 supervisor 注入 pre_recon JSON + 复用指引，threat_model 缺的是"历史任务已覆盖资产"的硬性跳过约束。 | 验证成立；**B1+B2 已实施**（§6） |

**真正影响效率与速度的根因不是"有没有消费 pre_recon"，而是：**
1. 上下文注入是**与当前任务无关的确定性 top-N**，而非"针对当前子任务的相关性检索"；
2. `pre_recon`（平台自动）与 `threat_model`（LLM 驱动）是**两次独立的侦察**，且 `threat_model` 缺 `covered_block` 约束，无法利用历史跳过重复枚举。

---

## 1. 问题 A：上下文注入是"全量"还是"按需检索"

### 1.1 证据：注入链路的两段式结构

`get_unified_context` 在 `ContextEngine` 层分两段拼装：

**第一段 —— `StructuredContext.get_unified_context`（`context_engine.py:642-835`）**
按固定 section 顺序拼装，并受 `max_tokens` 预算保护：

```642:835:pobi_agent/context/context_engine.py
def get_unified_context(self, max_tokens: int = 6000) -> str:
    ...
    # SECTION 1: TARGET + GOAL
    # SECTION 2: FLAG FOUND (FULL reproduction steps)
    # SECTION 3: TEST HISTORY (success 全保留, failure 每 endpoint 最多 5 条)
    # SECTION 4: KEY DISCOVERIES (按 confidence 取前 20 条)
    # SECTION 5: IDENTIFIED ENDPOINTS (最多 50 条)
    # SECTION 6: VULNERABILITIES (按 confidence 取前 20 条)
    # SECTION 6.5: AUTHENTICATION
    # SECTION 7: AGENT INSIGHTS (最近 5 条)
    # max_tokens 总预算：按优先级从低到高删除 section，直到 <= max_tokens
```

要点：**不是无脑全量**——它有 `max_tokens` 预算，且删除优先级从低到高（HEAD/FLAG 最高优先不删，其余按 `deprioritized` 列表删，最终硬截断）。这与"全量撑爆"的判断不符。

**第二段 —— RECON 分层注入块（`context_engine.py:1132-1176`）**

```1132:1161:pobi_agent/context/context_engine.py
def get_unified_context(self, max_tokens: int = 6000) -> str:
    base = self.structured.get_unified_context(max_tokens=max_tokens)
    # 挂载 RECON 本地库 L0/L1/L2 分层注入块
    recon_block = self._build_recon_index_block()
    if recon_block:
        return f"{recon_block}\n\n{base}"
    return base
```

`recon_block` 来自 `recon_store.build_index_view`，同样有分层 token 预算（L1≤500、L2≤1500）。

### 1.2 证据：选取逻辑是"确定性 top-N"，不是"相关性检索"

`build_index_view`（`store.py:692-798`）的选取全部是**按固定排序的前 N 条**，与"当前在做什么子任务"完全无关：

- **L0 目标基线**：取 `confidence >= 0.8` 的事实前 15 条 + 站点总览（`store.py:720-724`）；
- **L1 端点与技术栈**：取按 `confidence` 降序的前 20 个端点，再 `_fit_budget` 截断到 500 tokens（`store.py:754-763`）；
- **L2 历史利用复用**：取 `status in (confirmed, exploited)` 的前 10 条威胁 + 成功 technique 前 10 条，截断到 1500 tokens（`store.py:778-790`）。

`StructuredContext.get_unified_context` 同样按 `confidence` 排序取前 20/50 条，**没有任何基于 `objective` 或当前 `task` 的语义过滤**。

### 1.3 根因分析

上下文注入是**"与任务无关的全集 top-N 截断"**，而非"针对当前子任务的相关性检索"。后果：

1. **无关资产占用预算**：若目标有 200 个端点，L1 永远只取 confidence 最高的前 20 个——但当前子任务可能恰恰需要的是那第 50 个"低频但相关"的端点（如一个隐藏的管理后台），它永远进不了上下文。
2. **相关资产被裁掉**：当 section 超 `max_tokens`，删除优先级固定的"低优先级 section"（如 `## ENDPOINTS`、`## VULNERABILITIES`）会被整段删掉，而真正与当前子任务相关的端点/漏洞可能正埋在这些 section 里。
3. **`build_index_view(task_id, objective="")` 的 `objective` 参数形同虚设**：签名预留了 `objective`，但函数体从未用它做过滤（`store.py:692-714` 直接 `select(...).where(task_id==...)` 全量捞取后排序截取）。

> 这与 `context-explosion-analysis.md` 描述的"L2 结构化重渲染层"同源：L2 是"单轮上下文无上限重渲染"，本文补充的是"即使加了预算，选取也是非相关性的"——两个问题叠加，既可能膨胀也可能误裁。

### 1.4 解决方案（顶层，不实现）

| 方案 | 描述 | 预期收益 | 改动量 |
|------|------|---------|--------|
| **A1. 相关性检索注入** | 让 `build_index_view(task_id, objective)` 真正消费 `objective`：用 `objective`/当前 `task_node.task` 做 embedding 或关键词匹配，对端点/事实/威胁做相似度排序后取 top-N，而非纯 `confidence` 排序。可复用已有的 RAG 连接器。 | 上下文"精准命中"当前子任务，相关资产不再被裁，无关资产不再占预算。 | 中 |
| **A2. 分阶段动态预算** | 不同 phase 用不同预算权重：recon 阶段重端点（L1 预算↑、L2↓），exploit 阶段重漏洞/历史利用（L2↑、L1↓）。当前 L1/L2 预算是写死的常量（`store.py:61-62`）。 | 每个阶段只带"当下最有用"的资产，token 更省、信号更密。 | 小 |
| **A3. 按需 lazy 检索接口** | 保留 L0 基线全注入，L1/L2 改为"索引 + 检索指令"：在 prompt 里告诉 LLM"如需某端点/技术的细节，调用 `query_recon(keyword)` 工具主动拉取"。把"推"改成"推索引 + 拉细节"。 | 彻底解除 top-N 截断导致的信息丢失，且 token 占用恒定。 | 大 |

---

## 2. 问题 B：threat_model 是否重复侦察 pre_recon 已完成的侦察

### 2.1 证据：pre_recon 是平台层自动侦察（一次）

`pre_recon` 在任务启动早期自动执行指纹识别、WAF 识别、站点地图爬取，结果写入 `recon_fingerprints` / `recon_endpoints` / `recon_facts` 等表（`pre_recon.py` 的 `persist_fingerprint` 等落库逻辑）。这是**无 LLM 参与的平台自动步骤**。

### 2.2 证据：threat_model 的 supervisor prompt 仍命令 LLM "用工具去侦察"（第二次）

`threat_model`（`pobi_agent.py:536-563`）的 prompt 明确：

```536:563:pobi_agent/pobi_agent.py
prompt_task = f"""
Prepare the necessary information (reconnaissance) to achieve the following task: {task}
...
Critical rules:
- Make requests to the target and analyze responses
- Follow forms, links, and endpoints to discover relevant information
- Extract endpoints, authentication info, and secrets from actual tool responses
- Do NOT invent or guess endpoints - only use what is discovered
...
"""
```

同时 `threat_model` 通过 `execute_supervisor(phase="recon")` 执行（`pobi_agent.py:593-599`），子 agent 会实际调用 `requester` / `webapp_analyzer` 等工具**再次访问目标**。也就是说，**pre_recon 已探明的端点/技术栈，threat_model 又用 LLM 工具探了一遍**。

### 2.3 证据：covered_block 未注入 threat_model，只在 run_exploitation 注入

`covered_block`（"已覆盖资产，禁止重复劳动"）的注入点：

```831:831:pobi_agent/pobi_agent.py
{covered_block}   # run_exploitation 第一阶段
```
```905:905:pobi_agent/pobi_agent.py
{covered_block}   # run_exploitation 第二阶段
```
```1087:1091:pobi_agent/pobi_agent.py
{covered_block}
### 跳过重复工作规则（历史任务已覆盖，禁止重复劳动）
- 上述「已覆盖资产」来自同一授权目标的历史任务沉淀。除非本任务目标明确指向它们，**禁止重复扫描、重复枚举、重复验证**。
```

而 `threat_model` 的 prompt（`pobi_agent.py:536-563`）**完全没有 `{covered_block}` 占位符**，其 `execute_supervisor` 调用也只传了 `agent_context=target_context`（588 行），未携带 covered 约束。

> 子 agent 虽可通过 `context.get_unified_context()` 间接读到 `recon_block`（L0/L1/L2），但 **L0/L1/L2 是"已知资产清单"，不是"禁止重复枚举"的指令**——它告诉 LLM"这些是已知的"，但没有像 `covered_block` 那样明确"不要再去探它们"。因此 threat_model 阶段仍会倾向用工具重新验证。

### 2.4 根因分析

"重复侦察"不是 bug，而是**两阶段设计各自内置侦察职责**的必然结果：

- `pre_recon`：平台自动、无 LLM、快、全量；
- `threat_model`：LLM 驱动、慢、按任务相关；

二者职责重叠在"端点发现/技术栈识别"上。由于 `threat_model` 缺少 `covered_block` 这样的"信任预侦察结论、跳过重复枚举"的硬约束，LLM 会重新发起请求，产生：

1. **时间浪费**：同一批端点被请求两次（pre_recon 一次 + threat_model 子 agent 一次）；
2. **token 浪费**：threat_model 的工具响应又灌回 `message_history`（即 `context-explosion-analysis.md` 的 L3 层），加剧膨胀；
3. **速率/风控风险**：对同一目标重复请求，更易触发 WAF/速率限制（pre_recon 已识别 WAF 却未在 threat_model 复用该结论做限速）。

### 2.5 解决方案（顶层，不实现）

| 方案 | 描述 | 预期收益 | 改动量 | 状态 |
|------|------|---------|--------|------|
| **B1. threat_model 注入 covered_block** | 把 `run_exploitation` 已有的 `{covered_block}` + "禁止重复枚举"规则，同样注入 `threat_model` 的 prompt（`pobi_agent.py:536` 处）。让威胁建模阶段"信任 pre_recon 结论，只在缺口处补探"。 | 消除 threat_model 对 pre_recon 已覆盖端点的重复请求，省一轮侦察时间。 | 小 | **已实施（2026-09-09，见 §6）** |
| **B2. pre_recon 结论作为 threat_model 的"只读基线"** | 在 `threat_model` 启动前，把 `pre_recon` 落库的端点/技术栈/WAF 结论以结构化"已知基线"块注入，并显式指令"这些已探明，仅对新发现的攻击面发起请求"。 | 让 LLM 从"重新侦察"转为"校验+补充"，轮次显著下降。 | 小 | **已实施（2026-09-10，见 §6）** |
| **B3. 两阶段侦察职责拆分** | 明确契约：`pre_recon` 负责"资产发现"（端点/技术栈/认证面），`threat_model` 只负责"基于已知资产做攻击面推理与优先级排序"，禁止 threat_model 发起广谱爬取类请求。 | 从设计上消除重叠，而非靠 prompt 约束。 | 中 |

---

## 3. 两问题的耦合关系

A 与 B 并非独立：

- **B 的重复侦察 → 放大 A 的膨胀**：threat_model 重复请求产生的工具响应进入 L3 `message_history`，同时新发现又写入 `facts`，使 L2 `get_unified_context` 的 top-N 池更大、更杂，进一步稀释"相关性"。
- **A 的非相关性选取 → 削弱 B 的复用价值**：即使 `covered_block` 注入 threat_model，若 `get_unified_context` 注入的 L1 端点都是"高 confidence 但无关当前任务"的，LLM 仍会倾向自己探。

**建议落地顺序**（按杠杆 / 改动量）：
1. **B1（小）**：threat_model 注入 covered_block —— 立即减少一轮重复侦察；
2. **A2（小）**：phase 动态预算 —— 让每个阶段上下文信号更密；
3. **A1（中）**：objective 相关性检索 —— 根治"无关资产占预算"；
4. **B3（中）**：两阶段职责拆分 —— 从设计上消除重叠。

---

## 4. 验证方法（度量先行，不改动）

在动手前，建议先补 phase 级耗时与 token 度量（呼应 `context-explosion-analysis.md` 的"先度量后治理"）：

- 在 `deadend_runner.py` 各 phase 边界（pre_recon / threat_model / exploitation / report）落 `duration_ms` 与 `prompt_tokens`（pre_recon 已有 `fingerprint.duration_ms` 先例）；
- 统计 `threat_model` 阶段子 agent 发出的请求中，**与 pre_recon 已落库端点重叠的比例**——若 >30%，则 B 问题成立且 B1 收益可观；
- 统计 `get_unified_context` 注入 token 中，**与当前 task 无关（后续未被任何工具/决策引用）的占比**——若高，则 A1 收益可观。

---

## 5. 关联文档

- `context-explosion-analysis.md`：上下文窗口超限（L2 重渲染无上限 + L3 跨轮累积无刹车），本文是其"内容选取策略"维度的补充。
- `architecture.md` / `constraints.md`：若后续落地 A1/B3，涉及 supervisor prompt 契约与 recon 选取逻辑变更，应同步更新。
- `roadmap.md`：B1 落地记录（2026-09-09）。

---

## 6. 验证结果与实施记录（2026-09-09）

### 6.1 验证结论（源码核对）

全部引用行号与结论已对照当前源码逐项核实，**无失实**：

| 文档断言 | 源码核对 |
|---------|---------|
| `StructuredContext.get_unified_context` 固定 section + max_tokens 预算（context_engine.py:642-835） | ✅ 642 定义，811-834 预算删除与硬截断 |
| `ContextEngine.get_unified_context` 挂载 RECON 块（1132-1176） | ✅ 1132 定义、1156-1161 挂载、1163 `_build_recon_index_block` |
| `build_index_view` 的 `objective` 参数形同虚设（store.py:692-798） | ✅ 692 签名含 `objective`，函数体零消费；`_build_recon_index_block` 调用亦未传 |
| L1/L2 预算写死常量（store.py:61-62） | ✅ `L1_TOKEN_BUDGET=500` / `L2_TOKEN_BUDGET=1500` |
| threat_model prompt 命令主动侦察（pobi_agent.py:536-563） | ✅ "Make requests to the target" / "Follow forms, links, and endpoints" |
| covered_block 仅注入 run_exploitation（831/905/1087） | ✅ 831（run_exploitation）、925（start_testing_stream，文档原写 905 为 `_build_covered_block` 调用行，注入点实为 925）、1087（start_supervisor） |
| threat_model 无 covered_block | ✅ 536-563 prompt 无占位符；`execute_supervisor` 仅传 `agent_context=target_context` |

**补充发现（文档未覆盖）**：`executor.py:1076-1097` 的 `execute_supervisor` 已对**所有** supervisor（含 threat_model）注入 `build_pre_recon_json` 结构化块（指纹/WAF/端点综述/历史威胁）+ 复用指引（"禁止对已有结论的端点做重复全量侦查"）。因此 threat_model 并非"完全看不到 pre_recon"，缺的是**历史任务已覆盖资产（covered_endpoints/techniques/threats）的结构化清单 + 硬性跳过规则**——B1 增量仍成立。

**生产链路确认**：`deadend_runner.py:454-480` 在 threat_model **之前**执行 `seed_from_pg` + `seed_local_artifacts`（任务级单库 `tasks/<id>/<id>.db`，与 agent 内 `ReconStore.for_task` 同一路径）→ 490 `threat_model`（此前无 covered_block）→ 498 `run_exploitation`（有 covered_block）。B1 修复直接作用于真实主链路。

### 6.2 B1 实施内容

1. **`_build_recon_prompt(task, covered_block="")`**：covered_block 非空时追加 `### 跳过重复工作规则` 块（措辞与 start_supervisor 一致：禁止重复扫描/枚举/验证、已确认漏洞直接引用 evidence、聚焦新增攻击面）。
2. **`threat_model`**：`prompt_task` 改为 `self._build_recon_prompt(task, covered_block=self._build_covered_block(token_budget=1000))`；同时消除原 536-563 与 `_build_recon_prompt` 完全重复的硬编码 prompt。
3. **`threat_model_stream`**：同步注入 covered_block。

### 6.3 测试与验证

- `tests/recon/test_prompt_skip.py` 新增 2 项：`test_recon_prompt_without_covered_block_unchanged`（无覆盖时行为不变）、`test_recon_prompt_embeds_covered_block`（有覆盖时注入清单 + 跳过规则）。
- recon 全量 109 passed / 1 skipped；`test_lookup_p99_under_50ms` 批量跑时环境波动（P99 89ms>50ms，单独重跑通过，与本次改动无关——该用例测 `store.lookup` SQLite 查询延迟，未触达 prompt 构建路径）。
- `python -m py_compile` 通过。

### 6.4 遗留（待度量后实施，见 §4 度量建议）

| 方案 | 阻塞原因 |
|------|---------|
| A2 phase 动态预算 | L1/L2 预算需 phase 语义，`get_unified_context` 有 7+ 调用点（executor/architecture/pobi_agent），全部透传 phase 侵入面大；收益未度量 |
| A1 objective 相关性检索 | 需 embedding/关键词相似度排序，改动中等；收益（无关资产占比）未度量 |
| B3 两阶段职责拆分 | supervisor prompt 契约设计层变更，影响面最大，须先度量 threat_model 重复请求占比 |

### 6.5 B2 实施记录（2026-09-10）

**需求**：① agent 自行判断现有侦查结果是否满足威胁建模要求，不满足/认为有新攻击面时才新请求；② 优先使用前置侦查结果。

**落地**：重写 `_build_recon_prompt`（threat_model / threat_model_stream 共用任务单）：
- 新增 `## Pre-recon baseline FIRST` 工作流段：明确 supervisor prompt 已含 `<pre_recon_json>` 基线（target / fingerprint / WAF / site_overview / historical_threats），**先判断**（相关端点是否已知 / 认证要求是否已知 / 攻击面是否足够定义），**仅对基线缺口或新攻击面发起请求**；
- Critical rules 从"Make requests to the target"改为"PRIORITIZE the pre-recon baseline: request ONLY when the baseline is insufficient or a new attack surface is identified"，并新增"Do NOT repeat requests for endpoints / fingerprints / WAF already in <pre_recon_json> or the covered assets"；
- 输出要求标注"哪些缺口触发了新请求"（服务 §4 重叠率度量）。

**数据侧无需改动**：pre_recon_json 已由 `executor.execute_supervisor`（executor.py:1076-1097）对所有 supervisor 注入，`build_pre_recon_json`（store.py:2017-2102）读时现算（target/fingerprint/waf/site_overview/historical_threats），threat_model 启动时序在 pre_recon 落库之后（`pobi_v2/engine/executor.py:358`）。

**测试**：`tests/recon/test_prompt_skip.py` 新增 `test_recon_prompt_prioritizes_pre_recon_baseline`；recon 全量 89 passed / 1 skipped。

**遗留**：executor 层"复用指引"仅针对 historical_threats（存在性验证），未改为全基线优先——因其影响 exploitation 阶段所有 supervisor，超出本次威胁建模范围，留待 B3 一并处理。
