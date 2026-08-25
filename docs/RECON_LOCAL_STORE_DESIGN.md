# 侦查阶段本地 SQLite 存储与按需消费方案（落地设计 v2）

> 状态：提案草案 v2.1（**本地基座 + PG 聚合层双主轴均已实现**，供后续 AI 落地参考；v2.1 修订双主轴存储：本地 per-task + PG per-target）
> 关联文档：`THREAT_MODEL_STORAGE_DESIGN.md`（已存在）、`PLATFORM_STATUS.md`（状态快照）、`PROJECT_GOAL.md`

**落地进度（2026-08-19 第一阶：本地物化 + 消费闭环，已合入）**
- ✅ 已实现：本地 SQLite 5 表（`recon_sessions/facts/endpoints/techniques/threats`，独立 `ReconBase`）、`ReconStore`（WAL + `foreign_keys` 连接级 + `user_version` 迁移 + 幂等 upsert + `lookup` + `build_index_view` L0/L1/L2 token 预算裁剪）、`ReconCrypto` secret 级 AES-GCM 占位接口（`recon/crypto.py`）。
- ✅ 已实现：`ContextEngine` 注入可选 `recon_store`，`add_discovered_fact` / `add_execution` / `record_attempt` 旁路非阻塞写入（失败仅 warning，no-op 安全）；`get_unified_context` 顶部挂载 L0/L1/L2 分层注入块。
- ✅ 已实现：`recon_lookup` 工具（`tools/recon_lookup.py`，按 `session_id` + `task_root` 定位本地库，命中短路探索），已在 `tools/__init__.py` 注册。
- ✅ 已补：单元测试 `tests/recon/`（模型 CRUD / 幂等 upsert / WAL / 按 task 隔离 / lookup 命中 / 分层 token 裁剪），21 项全绿。

**落地进度（2026-08-19 第二阶：PG 聚合层 + 基线续扫 + 事件钩子同步，已合入）**
- ✅ 已实现：PG 聚合层 `recon_facts_agg` / `recon_threats_agg` / `recon_threat_evidence_link`（`pobi_v2/db/recon_models.py`，挂 `Base`，`target_id` UUID 对齐 `Task.target_id`，含聚合唯一约束保证跨任务收敛）；alembic 迁移 `0014_recon_agg.py` 建表可回滚，并在 `pobi_v2/db/models.py` 注册元数。
- ✅ 已实现：事件钩子同步——`PobiV2EventHooks.emit_recon_upsert`（扩展方法，不污染 `EventHooks` Protocol）+ 独立 `recon_sync_worker` 订阅 `__recon_sync__`，异步 `INSERT ... ON CONFLICT DO UPDATE` 增量聚合到 PG（失败仅 warning）。本地库旁路写入成功后经 `ContextEngine._recon_emit_sync` 触发。
- ✅ 已实现：基线续扫——`ReconStore.seed_from_pg(target_id, tenant_id, async_session_factory)` 拉取目标历史沉淀灌入本地库；`PobiAgent.start_supervisor` 启动前调用预热，`get_unified_context` 自动重新挂载 L0/L1/L2；`PobiAgent.__init__` 注入 `target_id`/`tenant_id` 与 `ReconStore`。
- ✅ 已实现：FTS5 辅助检索（v2.1 修订点 #4）——`recon_facts_fts` 虚拟表 + `lookup(keyword=)` 走 FTS5 MATCH 增强召回；`upsert_fact` 同步维护索引。
- ✅ 已实现：v2.1 修订点 #2——`recon_threat_evidence_link` 多对多中间表已建（PG 聚合层），本地库 threat↔evidence 仍 1:1 摘要外置（证据加密占位沿用 `ReconCrypto`）。
- ✅ 已补：集成测试门禁 `tests/recon/test_integration.py`——合成 220 fact + 50 threat，验证 `build_index_view` token 上限、lookup P99<50ms、WAL 崩溃恢复、PG 同步收敛（mock）、迁移可应用（需 `POBI_TEST_PG_URL`，无 PG 时 skip）。全套 25 passed / 1 skipped。

**落地进度（2026-08-19 第三阶：增量续扫 + 威胁状态机 + 跳过重复工作闭环，已合入）**
- ✅ 已实现：**增量续扫裁剪**——`seed_from_pg` 返回 `SeedResult`（`seeded_count` / `already_covered_count` / `covered_endpoints` / `covered_techniques` / `covered_threats`），同键记录不重复灌入（仅高置信度覆盖）；新增 `covered_assets()` 只读统计与 `build_covered_block()`（渲染「已覆盖资产（跳过重复工作）」prompt 块，≤token 预算确定性裁剪）。`start_supervisor` 预热日志区分新增/已覆盖。
- ✅ 已实现：**威胁状态机**——`update_threat_status()` 单向推进 `suspected→confirmed→exploited→remediated`（降级拒绝、`exploited` 必须带 evidence、空 cve_id 回退 `affected_endpoint` 定位）；`reconcile_pg_to_local()` 按状态机顺序做 PG 权威反向对账（升级/保留/灌入，返回摘要）。`ContextEngine.record_threat_status()` 公开入口 + `_promote_threat_on_success`（attempt 成功且定位到 CVE 时提升 exploited）。
- ✅ 已实现：**prompt 显式跳过**——`PobiAgent._build_covered_block()` 统一渲染，三阶段注入：`start_supervisor` 主 prompt、`run_exploitation` exploit_context（Previous Reconnaissance Results 内）、`start_testing_stream`（补齐缺失的 previous_context + 覆盖块），均含「禁止重复扫描/枚举/验证」规则。
- ✅ 已补：单元测试 `test_incremental_seed.py`（覆盖清单/幂等/高置信度覆盖/渲染预算）、`test_state_machine.py`（状态机合法/降级/证据要求/反向对账）、`test_prompt_skip.py`（渲染与三阶段模板契约）；`test_integration.py` 追加 seed→增量 upsert→reconcile 往返闭环。全套 49 passed / 1 skipped（迁移用例需 `POBI_TEST_PG_URL`）。

**基线续扫数据流**
```
新任务启动(target_id)
  → PobiAgent.start_supervisor → recon_store.seed_from_pg(target_id)
  → PG recon_facts_agg/recon_threats_agg 按 target 拉取历史沉淀
  → 灌入 per-task SQLite + 触发 ContextEngine L0/L1/L2 自动重挂
运行时：
  ContextEngine.add_* 旁路写入 SQLite
  → _recon_emit_sync → event_hooks.emit_recon_upsert → __recon_sync__
  → recon_sync_worker 异步 upsert 到 PG（跨任务收敛）
```

**剩余可选项（非阻塞）**
- `ReconThreatEvidenceLink` 与本地库 evidence 的双向同步（当前仅 PG 聚合层建表，本地↔PG evidence 关联未自动填充）。
- 真实 PG 环境下的迁移/同步 e2e 验证（CI 注入 `POBI_TEST_PG_URL`）。
- secret 级 evidence 的真实密钥管理（KMS/环境变量统一派生），当前为 AES-GCM 占位降级明文。
> 重要声明：本文档为**尚未落地的方案提案**。除「§0.1 当前代码实际状态」一节外，其余章节（架构、数据模型、改造步骤等）描述的均为**目标态设计**，不代表代码现状。任何落地工作必须以「§0.1」列出的真实代码结构为起点，不得假定 recon 相关表/类/工具已存在。

---

## 0. 背景与问题陈述

### 0.1 当前代码实际状态（已核实，落地起点）

> 以下事实基于对 `context_engine.py` / `sqlite_models.py` / `session_manager.py` / `recon_threatmodel_agent.py` / `pobi_agent.py` / `event_bus.py` 的源码核查（2026-08-19）。落地时如源码已演进，须重新核对。

1. **侦查产物以内存对象 + 文本文件沉淀，不落 SQLite**：
   - `ContextEngine.structured`（`StructuredContext`）持有 `facts: Dict[str, DiscoveredFact]`、`executions: List[ExecutionRecord]`、`thoughts: List[AgentThought]`，均为**进程内内存**，无 SQLite 读写。
   - `ContextEngine` 另将对话流持久化到文本文件 `tasks/<task_id>/agent/<agent_id>/<session_id>/run_context/context.txt`（`context_file_path`），**非结构化、非 SQLite、不可按需查询**。
   - `ContextEngine` 有内存容量上限：`_max_executions=50`、`_max_thoughts=20`、`_max_log_chars=50000`，超出即裁剪，存在信息丢失。
2. **注入形态为「整段散文」，且带硬截断**：
   - 利用阶段通过 `ContextEngine.get_unified_context(max_tokens=6000)`（默认 6000）拼装文本，按 section 顺序组装，**超出 `max_tokens` 不报错但靠后 section 内容会被压缩/丢弃**（如 SECTION 8 NEXT STEPS 等）。
   - 另有 `ContextEngine.get_all_context(max_tokens=8000)`（默认 8000）走 `get_executor_context`，用于某些调用处。两个入口默认值不同，需注意。
   - `PobiAgent.threat_model()` 将 `ContextEngine` 的上下文字符串注入 reporter prompt 生成 `recon_report.md`，后续利用阶段读取该报告文本（散文），**不保留结构化字段**。
3. **无按需精确查询能力**：Supervisor 只能消费固定字符串，无法按 ID / 关键字精确取回某条侦察记录。
4. **不可对账**：威胁建模断言与验证结论无逐项核对机制，仅依赖 prompt 软约束。
5. **存储现状（核实）**：
   - **PG**（`pobi_v2/db/`）：承载业务主数据 `Target/Task/TaskEvent/Finding/Artifact` 等（见 `PLATFORM_STATUS.md` §2.6）。**没有** `ReconSyncLog` 表；alembic 历史仅含 `0002_m3_persistence`。
   - **本地 SQLite**（`pobi_agent/rag/sqlite_models.py`）：仅定义 `CodeChunkSqlite` 一张表（RAG 代码块 + embedding BLOB），**没有任何 `recon_*` 表**。
   - **RAG 库路径隔离**（`RagSessionManager`）：实际目录布局为 `{storage_root}/{agent_id}/{embedding_session_id}/{target_slug}.db`，并配 `.manifest.json`，**不是** `tasks/<task_id>/recon/recon.db`。复用前须注意此差异。
6. **真实入口/模型（落地锚点）**：
   - 威胁建模入口：`PobiAgent.threat_model(task)`。
   - **重要**：`threat_model()` 实际调用的是 `self.executor.execute_supervisor(...)`（一个**通用 supervisor**，定义在 `agents/components/executor.py`），**并非** `ReconThreatModelAgent` 类。`ReconThreatModelAgent`（`agents/recon_threatmodel_agent.py`，输出 `ThreatModelOutput = PlannerOutput + GeneralInfoOutput`、仅暴露 `webapp_code_rag` 工具）目前只是被实例化挂在 `WorkflowStopResult.threat_model_agent` 上，**并未接入 `threat_model` 执行流程**；侦察+威胁建模的结果靠通用 supervisor 跑完后由一个 `ReporterAgent` 把 `ContextEngine` 的上下文字符串总结成 `recon_report.md`。
   - **子 agent 编排链路（已核实）**：`execute_supervisor` 在每轮迭代里实例化子 agent（requester / authenticator / shell / python_interpreter 等），并把这些子 agent 与**同一个 `self.context`（`ContextEngine` 实例）共享**（`executor.py` 把 `context` 透传给子 agent）。子 agent 产出经由两条路径进入 `ContextEngine`：
     1. supervisor LLM 返回的 `detailed_summary / proofs / thoughts` 字段 → `context.add_discovered_fact(...)` / `add_thought(...)`（`executor.py:505-540` 附近的 summarizer 回调）；
     2. 子 agent 内部也可直接调 `context.add_*`（因 `context` 已透传）。
   - **侦察 → 利用的数据连续性（已核实）**：侦察阶段与利用阶段共享**同一个 `self.context` 实例**。`run_exploitation` 调用 `get_unified_context(max_tokens=4000)` 取出侦察沉淀，再 `clear_current_log()`（保留 `facts/executions/thoughts`），拼入 `exploit_context`（`pobi_agent.py:729-758`）。`start_testing_stream` 则 `self.context.reset()` 完全清空。→ 说明落库对象在内存中天然延续到利用阶段，**无需另造数据桥接**。
   - 利用入口：`run_exploitation(threat_model, task)`（当前签名无 `recon_index` 参数）。
   - 内存源结构：`DiscoveredFact` / `ExecutionRecord` / `AttemptRecord` / `AgentThought` 均为 `@dataclass`（在 `context_engine.py`），**非 SQLAlchemy 模型**，落库需做 schema 映射。`DiscoveredFact.category` 是**自由字符串**（非枚举），落库时须做 `normalize_category` 归一化（见 §2.1）。
   - `Severity` 枚举真实存在于 `pobi_v2/db/models.py`，可直接复用。
   - 事件转发：`PobiV2EventHooks`（`pobi_v2/engine/event_bus.py`）仅做事件→总线/SSE 转发（含 `persist_event_worker` 写 `TaskEvent`），**无任何 recon 同步、脱敏、`ReconSyncLog` 逻辑**。

### 0.2 问题本质

- 问题 = **截断丢信息 + 无结构 + 无按需**，解法方向 = **全量结构化落盘 + 分层短目录 + 工具按需取 + 状态机对账**。
- 存储选型方向 = **单任务内容落到本地 SQLite（primary store，per-task，零网络延迟）；PG 作为 per-target 聚合/审计/跨任务记忆层（实时 upsert 同步，供新任务拉基线）**。
- 注入形态改造方向 = 从「整段散文注入」改为「**L0 历史基线 + L1 常驻摘要 + L2 动态相关详情 + `recon_lookup` 工具**」。

### 0.3 v2 修订重点（来自实战评审的待规避问题）

| # | 待规避问题 | v2 处置方向 |
|---|---|---|
| 1 | `recon_index` token 失控（200 条→6k+） | L1/L2 分层索引 + 相关性过滤 + token 硬上限 + 确定性截断（§2.4, §4.1） |
| 2 | Evidence↔ThreatEntry 多对多缺失（JSON 外键反范式） | 新增 `recon_threat_evidence_link` 中间表，移除冗余外键字段（§2.2, §2.3） |
| 3 | SQLite 无迁移机制 / WAL 崩溃丢数据 | `PRAGMA user_version` 增量迁移 + `wal_checkpoint(TRUNCATE)` + 固化连接串（§3.3, §9.1） |
| 4 | category 枚举爆炸导致漏召回 | `ReconCategory` 枚举 + 写入归一化 + FTS5 辅助检索（§2.1, §4.2） |
| 5 | Evidence 原文含密泄漏 | `sensitivity` 分级 + secret 级 AES-GCM 加密（本地库，密钥仅内存）+ `.db` 权限 600；PG 同步本地原始数据（非脱敏副本），业务出口 `Finding`/`Artifact` 才做合规脱敏（§2.3, §5.4） |
| 6 | 单文件故障域 / 大文本膨胀 / 外键不约束 / 状态机非法跃迁 | 大文本外置 blob + `PRAGMA foreign_keys=ON` + 状态机合法流转图谱（§3.2, §2.2, §9.1） |

### 0.3.1 v2.1 主轴修订（2026-08-19，断点续扫驱动）

| # | v2 原设计 | v2.1 修订 | 理由 |
|---|---|---|---|
| 7 | 本地 SQLite 按 `target_slug` 共享同一 `.db`，跨任务 upsert 去重 | **本地 SQLite 改为 per-task（每任务独立 `.db`）；跨任务去重上移到 PG 聚合层 `recon_facts_agg`/`recon_threats_agg`（per-target，upsert）** | 本地 per-task 天然故障隔离；PG 已承载 `Target` 主数据，目标维度记忆与 `Target` 对齐；去重复杂度收敛到同步层（§3.1, §2.8, §4.4, §5.1） |
| 8 | 续扫时读本地 `target_slug.db` 作基线 | **新任务从 PG `load_baseline_from_pg(target_id)` 拉基线，新建本地 per-task 库注入 L0** | 本地库不跨任务读，重发任务不复用半截旧库；PG 是跨任务记忆唯一权威（§4.4） |

### 0.4 现有 agent 链路与方案改造点映射（落地边界，已核实）

> 结论先行：**现有数据流能支撑本方案，不需新增 agent，也不需改 supervisor / 子 agent 编排逻辑。** 仅需在「侦察产出侧」加一条旁路落库、在「利用注入侧」改 `exploit_context` 组装方式。

**真实链路（不变）**：
```
PobiAgent.threat_model(task)
   └─ execute_supervisor(通用, executor.py)   ── 每轮实例化子 agent(requester/authenticator/shell/python_interpreter...)
         └─ 子 agent 共享 self.context(ContextEngine)  ── add_discovered_fact / add_thought / record_attempt
              └─ ReporterAgent 把 context 字符串 → recon_report.md
run_exploitation(同一 self.context)  ── get_unified_context(4000) + clear_current_log() → exploit_context
```

**改造点映射表（明确边界）**：

| 改造目标 | 涉及文件 | 是否改 agent 逻辑 | 说明 |
|---|---|---|---|
| ① 侦察产物实时落库 | 新增 `recon_store.py`（`ReconStore`）+ `sqlite_models.py`（5 表） | ❌ 不改现有 agent | 在 `execute_supervisor` 的 summarizer 回调（`executor.py:505-540`）或 `ContextEngine.add_*` 处挂**旁路写入**，对 agent 透明 |
| ② 利用阶段注入改造 | `pobi_agent.py:747-759`（`exploit_context` 组装） | ❌ 不改 supervisor/子 agent | 把 `get_unified_context(4000)` 散文替换为「L1+L2 + `recon_lookup` 工具描述」 |
| ③ `recon_lookup` 工具注册 | `pobi_agent.py` / supervisor 工具表 | ❌ 不改编排，仅注册工具 | 新增工具函数，挂到利用阶段 supervisor 工具集 |
| ④ 状态机回写 | `recon_store.py`（`update_status`） | ❌ | 利用阶段验证结论回写，不牵动 agent |
| ⑤ PG 同步 | `event_bus.py`（事件钩子）/ 新增 `ReconSyncLog` | ⚠️ 仅新增钩子，不改既有转发 | `PobiV2EventHooks` 现有逻辑保持不动，在其上叠加分层同步钩子 |
| **是否新增 agent** | — | **否** | `ReconThreatModelAgent` 已存在但当前未接入；方案不依赖启用它，保持现状即可 |

**关键约束（务必遵守）**：
- **落库必须「实时增量」而非「阶段结束一次性 dump」**：`ContextEngine.structured` 有内存裁剪（`_max_executions=50` / `_max_thoughts=20` / `_max_log_chars=50000`），长任务/大目标在侦察阶段就会丢数据。若只在 `threat_model()` return 前 dump，丢掉的记录落盘也救不回。→ 落库应挂在 `ContextEngine.add_*` / summarizer 回调处**逐条写入**（或批量微批写入），使本地 SQLite 成为**权威全量副本**，内存仅作热缓存。
- **不改造 `execute_supervisor` 编排**：子 agent 的发现机制（哪些子 agent、如何被驱动）与方案无关；任何改动都应保持对现有编排「零侵入」，避免回归现有侦察能力。
- **`ReconThreatModelAgent` 的取舍**：当前用通用 supervisor 产出侦察+威胁建模。是否启用 `ReconThreatModelAgent` 是独立决策，**不在本方案强制范围内**；落地时优先保持现状，待 recon 落库稳定后再评估是否切换。

---

## 1. 总体架构（目标态）

```
┌──────────────────────────────────────────────────────────────────┐
│               侦查阶段 (Recon / Threat Model)                       │
│  facts / executions / thoughts (ContextEngine 内存)  +  ThreatModelOutput │
│  ※ 现状：内存 + context.txt；目标：全量序列化落本地 SQLite           │
└───────────────────────────┬──────────────────────────────────────┘
                            │ ① 全量序列化（不截断，大文本外置 blob）
                            ▼
┌──────────────────────────────────────────────────────────────────┐
│            本地 SQLite —— per-task 增量工作区 (PRIMARY STORE, 目标态)  │
│  路径: {root}/{agent_id}/{embedding_session_id}/{target_slug}/{task_id}.db │
│  约束: foreign_keys=ON, WAL+TRUNCATE, user_version 迁移（详见 §3.1）   │
│                                                                   │
│  recon_facts           侦查事实（category 枚举归一化，per-task 全量）【待新建】│
│  recon_threat_entries  威胁条目（status 状态机 + 合法流转校验）【待新建】│
│  recon_evidence        原始证据（sensitivity 分级 + 大文本外置）【待新建】│
│  recon_threat_evidence_link  多对多关联（JOIN 检索）【待新建】        │
│  recon_index_fts5      FTS5 虚拟表（自然语言模糊检索）【待新建】      │
│  code_chunks           (既有 RAG 向量，复用同一 .db)                │
│  blobs/                超大原始响应外置文本（>100KB）【待新建】        │
└─────┬──────────────────────────────────────┬───────────────────────┘
      │ ② L0基线(PG拉取)+L1摘要(<500tok)+L2动态(<1500tok) │ ③ recon_lookup(id/关键字)
      ▼                                  ▼
┌──────────────────────────┐      ┌──────────────────────────────────┐
│  利用阶段 Supervisor       │      │  recon_lookup 工具（精确检索+防护） │
│  exploit_context = L0+L1+L2│      │  单轮上限 + 非法ID友好提示          │
└───────────┬──────────────┘      └──────────────────────────────────┘
            │ ④ 验证状态回写（状态机合法流转）
            ▼
┌──────────────────────────────────────────────────────────────────┐
│  对账：侦察断言 ↔ 验证结论（build_reconciliation 接口，待实现）        │
└───────────┬───────────────────────────────────────────────────────┘
            │ ⑤ 分层同步（实时增量 upsert + 终态全量快照，secret 脱敏）
            ▼
┌──────────────────────────────────────────────────────────────────┐
│   PostgreSQL —— per-target 聚合/审计/跨任务记忆层（实时同步，目标态）   │
│  recon_facts_agg(per-target upsert) / recon_threats_agg(per-target upsert) │
│  Finding(仅 verified/exploited) / ReconSyncLog(全量归档,待建表)         │
│  ↑ load_baseline_from_pg(target_id) 供新任务拉基线（§4.4）              │
└──────────────────────────────────────────────────────────────────┘
```

**关键约束**：利用阶段（LLM 查询路径）**只碰本地 SQLite**，不触 PG。新任务启动前的基线预热（`load_baseline_from_pg`）是唯一的 PG 读取点，且发生在 supervisor 实例化子 agent 之前。多任务并行时本地 `task_id` 目录须规范化绝对路径 + 越界校验，禁止动态字符串拼接跨任务读库（§9.3）。

---

## 2. 本地 SQLite 数据模型（目标态设计 v2）

> 设计原则：
> - 贴近 `DiscoveredFact` / `ExecutionRecord` 既有**内存 dataclass** 字段，做落库 schema 映射，不另造语义。
> - **开启 SQLite 外键约束**（`PRAGMA foreign_keys=ON`），杜绝孤儿记录。
> - 多对多关联走中间表 + JOIN，消除 JSON 外键反范式。
> - category / kind / status / severity 一律 `Enum`，写入归一化。
> - 大文本（>100KB）外置 `blobs/`，DB 仅存路径+摘要，防膨胀。
> - 敏感证据分级 + 可选加密。
> - 同一 `.db` 复用既有 `code_chunks`（RAG）。
>
> 注：`severity` 字段复用真实枚举 `Severity`（`pobi_v2/db/models.py`）；其余 `Recon*` 枚举与表均**为新增**。

### 2.1 枚举与归一化（待新增）

```python
class ReconCategory(str, Enum):
    """侦查事实类别——强制枚举，禁止 LLM 自由填 String。"""
    tech_stack = "tech_stack"        # 技术栈
    endpoint = "endpoint"            # 端点/路由
    param = "param"                  # 参数
    auth = "auth"                    # 认证机制
    vuln_hint = "vuln_hint"          # 漏洞线索
    asset = "asset"                  # 资产/目录/子域
    other = "other"

class ReconEvidenceKind(str, Enum):
    payload = "payload"
    request = "request"
    response = "response"
    cookie = "cookie"
    header = "header"
    log = "log"
    screenshot_ref = "screenshot_ref"
    other = "other"

class ReconSensitivity(str, Enum):
    public = "public"      # 可明文入库、可同步 PG
    internal = "internal"  # 内部信息，同步 PG 时脱敏
    secret = "secret"      # 密码/Token/PII/API Key，AES-GCM 加密 + 文件权限600

# 别名归一化表（写入时 case-insensitive 映射）
CATEGORY_ALIASES = {
    "sql injection": "vuln_hint", "sqli": "vuln_hint", "sql_injection": "vuln_hint",
    "xss": "vuln_hint", "cross site scripting": "vuln_hint",
    # ... 视实战补充
}

def normalize_category(raw: str) -> ReconCategory:
    key = (raw or "").strip().lower()
    if key in CATEGORY_ALIASES:
        return ReconCategory(CATEGORY_ALIASES[key])
    try:
        return ReconCategory(key)
    except ValueError:
        return ReconCategory.other
```

### 2.2 表：`recon_facts`（待新建）

```python
class ReconFact(Base):
    __tablename__ = "recon_facts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    task_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)  # 本地库即 per-task（§3.1），task_id 为主维度
    target_slug: Mapped[str] = mapped_column(String(255), nullable=False, index=True)  # 仅标记所属目标，便于同步 PG 时定位（去重收敛在 PG 层，见 §2.8/§5）
    category: Mapped[ReconCategory] = mapped_column(
        SAEnum(ReconCategory), nullable=False, index=True
    )
    key: Mapped[str] = mapped_column(String(512), nullable=False)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    details: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    source_task: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    confidence: Mapped[float] = mapped_column(Float, default=0.5, nullable=False)
    actionable: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_noise: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # 噪声标记：索引视图默认过滤；lookup(include_noise=True) 可强制包含
    persist: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # 低置信/调试噪声可选择性不落库（仅留内存），便于回放
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    # 注：本地 per-task 库不做跨任务唯一约束；同 (target_slug,category,key) 去重由 PG 聚合层 upsert 承担（§2.8/§5.1）
```

### 2.3 表：`recon_threat_entries`（状态机 + 合法流转，待新建）

```python
class ReconThreatStatus(str, Enum):
    pending = "pending"
    in_progress = "in_progress"
    verified = "verified"        # 已验证存在，未利用
    exploited = "exploited"      # 已成功利用
    rejected = "rejected"        # 已排除（误报/不可达，终态）

# 合法状态流转图谱（update_status 内部强制校验，禁止非法跃迁）
VALID_TRANSITIONS: dict[ReconThreatStatus, set[ReconThreatStatus]] = {
    ReconThreatStatus.pending:     {ReconThreatStatus.in_progress, ReconThreatStatus.rejected},
    ReconThreatStatus.in_progress: {ReconThreatStatus.verified, ReconThreatStatus.exploited, ReconThreatStatus.rejected},
    ReconThreatStatus.verified:    {ReconThreatStatus.exploited, ReconThreatStatus.rejected},
    ReconThreatStatus.exploited:   {ReconThreatStatus.verified},  # 可重复验证
    ReconThreatStatus.rejected:    set(),  # 终态
}

class ReconThreatEntry(Base):
    __tablename__ = "recon_threat_entries"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    task_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    category: Mapped[ReconCategory] = mapped_column(SAEnum(ReconCategory), nullable=False, index=True)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    suggested_payload: Mapped[str | None] = mapped_column(Text, nullable=True)
    target_endpoint: Mapped[str | None] = mapped_column(String(1024), nullable=True, index=True)
    confidence: Mapped[float] = mapped_column(Float, default=0.5, nullable=False)
    severity: Mapped[Severity] = mapped_column(SAEnum(Severity), default=Severity.info, nullable=False, index=True)
    cwe: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[ReconThreatStatus] = mapped_column(
        SAEnum(ReconThreatStatus), default=ReconThreatStatus.pending, nullable=False, index=True
    )
    verification_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, onupdate=datetime.now)
```

### 2.4 表：`recon_evidence`（分级 + 大文本外置，待新建）

```python
class ReconEvidence(Base):
    __tablename__ = "recon_evidence"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    task_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    kind: Mapped[ReconEvidenceKind] = mapped_column(SAEnum(ReconEvidenceKind), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    # 原文：<=阈值(100KB)存 content；超限存 blobs/ 路径，content 仅留摘要
    content: Mapped[str] = mapped_column(Text, nullable=False)
    blob_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    # secret 级：content 为 AES-GCM 密文（密钥仅内存），lookup 按需解密
    sensitivity: Mapped[ReconSensitivity] = mapped_column(
        SAEnum(ReconSensitivity), default=ReconSensitivity.public, nullable=False, index=True
    )
    encrypted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    content_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    source: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
```

### 2.5 表：`recon_threat_evidence_link`（多对多，消除反范式，待新建）

```python
class ReconThreatEvidenceLink(Base):
    __tablename__ = "recon_threat_evidence_link"
    threat_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("recon_threat_entries.id", ondelete="CASCADE"),
        primary_key=True,
    )
    evidence_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("recon_evidence.id", ondelete="CASCADE"),
        primary_key=True,
    )
    relevance_note: Mapped[str | None] = mapped_column(String(512), nullable=True)
```

> 移除 `ReconThreatEntry.evidence_ids`（JSON 数组）与 `ReconEvidence.threat_entry_id`（单值 FK）。所有联表走 JOIN，O(log n) 检索。

### 2.6 FTS5 虚拟表（弥补 category 分类不准，待新建）

```sql
CREATE VIRTUAL TABLE IF NOT EXISTS recon_index_fts5 USING fts5(
    fact_key, fact_value, threat_title, threat_desc, evidence_title, content=''
);
```

`lookup(keyword=...)` 优先走 FTS5 MATCH；category 命中失败时可自然语言模糊召回。

### 2.7 复用与映射（源结构为内存 dataclass，非 DB 模型）

| 本地表（待建） | 既有源结构 | 说明 |
|---|---|---|
| `recon_facts` | `DiscoveredFact`（内存 dataclass） | 字段映射 + `category` 枚举归一化 + `is_noise`/`persist` |
| `recon_threat_entries` | `ThreatModelOutput`（= `PlannerOutput`+`GeneralInfoOutput`） | 展开 + 状态机合法流转 |
| `recon_evidence` | `ExecutionRecord.details` / 响应摘录 | 原文 + 分级 + 大文本外置 |
| `recon_threat_evidence_link` | （新增） | 多对多关联 |
| `code_chunks` | 既有 RAG（`CodeChunkSqlite`） | 不变，复用同一 .db |

### 2.8 PG 聚合层模型（per-target，待新建，v2.1 新增）

> 背景：本地 SQLite 已改为 per-task（§3.1），跨任务记忆须上移到 PG。新增以下两张表（alembic 迁移），按 `target_id` 聚合，作为 `load_baseline_from_pg`（§4.4）的数据源与 `sync_to_pg` 的 upsert 目标。

```python
class PGReconFact(Base):
    __tablename__ = "recon_facts_agg"   # PG 聚合层，区别于本地 recon_facts

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    target_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("targets.id", ondelete="CASCADE"), nullable=False, index=True
    )  # 聚合维度（授权目标）
    category: Mapped[ReconCategory] = mapped_column(SAEnum(ReconCategory), nullable=False, index=True)
    key: Mapped[str] = mapped_column(String(512), nullable=False)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    details: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    confidence: Mapped[float] = mapped_column(Float, default=0.5, nullable=False)
    actionable: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    source_tasks: Mapped[list] = mapped_column(JSON, default=list)  # 历次写入任务 ID 数组
    first_seen: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    last_seen: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        UniqueConstraint("target_id", "category", "key", name="uq_pg_fact_target_cat_key"),
    )  # 跨任务去重，避免目标聚合库膨胀（§4.4.3 / §5.1）

class PGReconThreat(Base):
    __tablename__ = "recon_threats_agg"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    target_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("targets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    category: Mapped[ReconCategory] = mapped_column(SAEnum(ReconCategory), nullable=False, index=True)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    target_endpoint: Mapped[str | None] = mapped_column(String(1024), nullable=True, index=True)
    confidence: Mapped[float] = mapped_column(Float, default=0.5, nullable=False)
    severity: Mapped[Severity] = mapped_column(SAEnum(Severity), default=Severity.info, nullable=False, index=True)
    cwe: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[ReconThreatStatus] = mapped_column(
        SAEnum(ReconThreatStatus), default=ReconThreatStatus.pending, nullable=False, index=True
    )
    source_tasks: Mapped[list] = mapped_column(JSON, default=list)
    first_seen: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    last_seen: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        UniqueConstraint("target_id", "target_endpoint", "category", name="uq_pg_threat_target_ep_cat"),
    )  # 同目标同端点同类别威胁唯一（落地可改 title_hash）
```

- 写入语义：`sync_to_pg` 对两表走 `ON CONFLICT DO UPDATE`，置信度取高 + `last_seen` 刷新 + `source_tasks` 追加（§5.1）。
- `ReconSyncLog`（既有规划，§5）保留作审计/失败重试记录，与聚合层解耦。

---

## 3. 存储后端选型与路径

### 3.1 双主轴存储：本地 per-task + PG per-target

> **v2.1 修订（2026-08-19）**：原 §3.1 建议「本地 SQLite 按 `target_slug` 共享同一 `.db`」已**废弃**。经评估，改为**本地 SQLite 按任务隔离（per-task），PG 按授权目标聚合（per-target）**的双主轴模型。理由：① 本地 per-task 库天然故障隔离，单任务崩溃不污染同目标其他任务（契合 §9.3）；② PG 本就承载业务主数据 `Target/Task/Finding`（§0.1 第 5 点），"目标维度历史沉淀"与 `Target` 实体天然对齐，无需在本地造跨任务聚合；③ 增量去重收敛到 PG 同步层（upsert），本地任务库保持纯增量工作区，无膨胀负担。

**本地 SQLite（PRIMARY STORE，per-task）**：

- 维度 = **`task_id`**（非 `target_slug`）。每个任务拥有独立 `.db`，任务结束 gzip 归档（§8 决策点 4），重发任务**不复用旧库**，而是新建 + 从 PG 拉基线（§4.4）。
- 路径方案（须对齐真实 `RagSessionManager` 隔离逻辑）：沿用其 `{storage_root}/{agent_id}/{embedding_session_id}/` 根，库文件按任务命名（建议 `{target_slug}__{task_id}.db` 或保持 `target_slug` 目录下 `{task_id}.db`），避免与现有路径治理冲突。最终以落地时 `RagSessionManager` 实际接口为准。
- 外置大文本：`blobs/<evidence_id>.txt`（相对 `.db` 同级目录）。

```
{storage_root}/{agent_id}/{embedding_session_id}/{target_slug}/{task_id}.db   # 本地任务库（WAL，含 code_chunks + recon_*，纯本次增量）
{storage_root}/{agent_id}/{embedding_session_id}/{target_slug}/blobs/<evidence_id>.txt
{storage_root}/{agent_id}/{embedding_session_id}/{target_slug}/{task_id}.db.gz  # 任务结束归档
```

**PostgreSQL（AGGREGATION STORE，per-target）**：

- 维度 = **`target_id`**（授权目标）。跨任务沉淀按目标聚合，新增 `ReconFact` / `ReconThreat` 表（§2.8），以 `(target_id, category, key)` 唯一约束 + upsert 收敛（见 §5）。
- 职责：跨任务记忆层 + 审计 + Web 控制台查询。本地任务库不跨任务读，新任务基线一律从 PG 拉取（§4.4）。

由 `ReconStoreManager`（复用 `RagSessionManager` 路径隔离逻辑）负责本地库创建/打开/归档；PG 侧由 `ReconSyncLog` + 新增 `ReconFact`/`ReconThreat` 表承载。

> **范围边界（明确排除）**：**登陆态 / 会话凭证（`AuthContext` / Playwright `storage_state` / cookies）维持现状，不纳入本 recon 存储方案。** 现有机制由 `AuthContextHandler` 持久化为本地 JSON（`~/.pobi/agents/<agent_id>/<session_id>/auth_context/<profile>.json` 或 `TASKS_ROOT/<task_id>/scope.*.yaml`），经 Playwright 内存单例 + `storage_state` 文件在任务内复用，与 `recon_*` 表正交。落地时**禁止**将 `AuthContext` / cookie / token 写入 `recon_facts` / `recon_evidence`（属 `sensitivity=secret` 范畴，且凭证生命周期与侦察事实不同——应保留独立的本地文件通道，不在 recon 库或 PG 聚合层留痕）。recon 数据中仅可记录「使用了哪个 `auth_profile` 完成认证」这一**引用标记**（如 `ExecutionRecord.parameters` 中的 `auth_profile` 字段），不存放凭证本身。

### 3.2 大文本外置策略（防 db 膨胀/损坏）

- 写入 `recon_evidence.content` 前校验：单条 > `BLOB_THRESHOLD=100KB` 时：
  - 原文写入 `blobs/<evidence_id>.txt`，`blob_path` 记路径；
  - `content` 仅存 ≤2KB 摘要（首尾截断）；`lookup` 命中时按需读 `blob_path` 全文返回（受 `max_return_bytes` 限制）。
- 业务层强制单条上限，防止超大 HTTP 响应触发磁盘耗尽 DoS。

### 3.3 SQLite 运行时加固（§9 详述）

- 连接串固化：`sqlite+aiosqlite:///<abs_path>?mode=wal&cache=shared`。
- `PRAGMA foreign_keys=ON`（每次连接）。
- 批量写后 `PRAGMA wal_checkpoint(TRUNCATE)` 确保落盘。
- `PRAGMA user_version` 驱动增量迁移，不依赖外部 json。
- 单任务生命周期维持**单一连接**，避免频繁 open/close；析构妥善关闭 + 清理 `-wal/-shm`。

---

## 4. 消费端改造：分层索引 + `recon_lookup` 工具（目标态）

### 4.1 分层索引（L1 + L2，强制 token 上限）

`build_index_view(current_objective: str, ...)` **不能无参**：

- **L1_Category_Summary（固定 <500 token，永远注入）**：按 category 聚合计数 + 各 status 分布，让 Supervisor 知道「有什么」。
  ```
  [SUMMARY] facts=182(threat=40,endpoint=62,auth=12,...) threats=53(pending=38,in_progress=4,verified=7,exploited=3,rejected=1)
  ```
- **L2_Relevant_Details（动态 <1500 token，按目标注入）**：依据 `current_objective` 做相关性过滤（FTS5 / LIKE / 轻量 embedding 相似度），返回 Top-K 相关条目。
  - 排序优先级：`status∈{pending,in_progress}` 优先 + `severity` 降序 + `confidence` 降序。
  - **硬上限**：`MAX_INDEX_TOKENS=2000`；超限走确定性截断（confidence 降序 + pending 优先），绝不全量输出。
  - 低置信/已排除/噪声默认折叠不注入，除非 `include_noise=True`。

### 4.2 `recon_lookup` 工具（精确检索 + 防护，待新增并注册）

```python
async def lookup(
    self,
    entry_id: str | None = None,        # 精确 ID (TE-3a / RF-12 / EV-7)
    keyword: str | None = None,         # FTS5 自然语言模糊
    category: ReconCategory | None = None,
    status: ReconThreatStatus | None = None,
    include_evidence: bool = True,
    include_noise: bool = False,
    max_results: int = 10,              # 单轮返回上限（防护溢出）
) -> str:
    """返回匹配条目结构化全文（含 evidence 原文/外置 blob）。"""
```

**防护机制（P0）**：
- `entry_id` 不存在 → 返回简短提示 `"[recon] no entry for <id>"`，**不抛堆栈**。
- `max_results` 硬性截断，超限提示「结果过多，请缩小范围」。
- Prompt 强约束：优先精确 ID 查询，少用语义模糊检索。
- secret 级证据默认返回 `[REDACTED]`，需显式 `reveal_secret=True` 且调用方在沙箱内才解密。

### 4.3 状态机回写与对账（目标态接口）

```python
recon_store.update_status(
    entry_id="TE-3a",
    status=ReconThreatStatus.exploited,   # 内部校验合法流转，非法抛 ValueError
    verification_note="FLAG{...} payload: ' OR 1=1--",
)
```

`build_reconciliation()` 规划为 `ReconStore` 原生接口，返回「断言↔结论」结构化数据，Reporter 仅负责格式化（逻辑与存储解耦）。

### 4.4 历史预热与增量复用（断点续扫，基于 PG 基线）

> **动机**：侦察任务可能因超时、配额、人工中断或 agent 崩溃而被迫终止。重新对**同一授权目标**发起任务时，AI 不应从零开始重扫已知端点，而应首先拉取该目标在 **PG 聚合层**（按 `target_id` 沉淀，见 §3.1 / §2.8）的已验证/高置信数据，作为新任务的「基线上下文」注入，仅对未知/未完成部分做增量侦查。新任务**不复用旧本地库**，而是新建 per-task 本地库（§3.1），开局即从 PG 拉基线。
>
> **v2.1 修订**：基线上源从「本地 `target_slug.db`」改为「PG `ReconFact`/`ReconThreat`（per-target）」。本地库已改为 per-task（§3.1），不再跨任务共享，故历史沉淀的读取职责完全移交 PG。

#### 4.4.1 增量闭环数据流

```
新任务启动 (target_id)
   └─ ReconStore.load_baseline_from_pg(target_id)   ── 拉 PG 该目标已验证/高置信沉淀
        └─ 写入新建的 per-task 本地库初始上下文 (L0 基线块)
             └─ execute_supervisor 实例化子 agent（开局即知"已探明 X / 待续 Z"）
                  └─ 子 agent 增量侦查（跳过已知，聚焦 unknown/pending）
                       └─ 本地库逐条写（本次增量）
                            └─ 增量事件 + 终态全量 → 同步 PG（upsert 收敛到 target_id）
```

- **本地库不跨任务读**：任务结束 gzip 归档（§8 决策点 4）；重发任务新建库 + 拉 PG 基线，逻辑最干净，无"复用半截库"的脏状态风险。
- **PG 是跨任务记忆唯一权威**：同目标第 N 次任务，基线来源始终是 PG 中该 `target_id` 的聚合结果。

#### 4.4.2 `load_baseline_from_pg(target_id)` 接口（目标态定义）

新任务启动时（在 `execute_supervisor` 实例化子 agent **之前**），调用 `ReconStore.load_baseline_from_pg` 从 PG 拉基线并注入新本地任务库：

```python
async def load_baseline_from_pg(
    self,
    target_id: str,                    # 授权目标 ID（PG 侧聚合维度，见 §2.8）
    include_noise: bool = False,       # 默认折叠历史噪声
    only_actionable: bool = True,      # 默认仅取 actionable / 高置信事实
    status_filter: set[ReconThreatStatus] | None = None,  # 默认仅拉 verified/exploited + 高置信 facts；不继承 pending/rejected
    max_facts: int = 300,              # 防护：PG 沉淀膨胀时硬截断
    max_threats: int = 100,
) -> BaselineView:
    """从 PG 拉取该 target 的跨任务基线，作为新任务的预置上下文。
    返回结构：{facts: list[PGReconFact], threats: list[PGReconThreat],
              summary: str,   # 人类可读的已覆盖摘要，供注入 supervisor（L0 基线块）
              last_task_id: str | None,    # 最近一次写入该目标的任务（断点提示）
              unfinished: list[PGReconThreat]}  # PG 中仍处 verified 待利用 / 高价值待续项
    """
```

- **注入位置**：`BaselineView.summary` 作为 **L0 基线块**注入 `execute_supervisor` 的 initial context（置于 L1 摘要之前），使 Supervisor 开局即知「已探明 X 端点、已验证 Y 漏洞、仍待续 Z 项」，从而跳过已知、聚焦未知。
- **`unfinished` 优先**：PG 中 `status∈{verified}` 且尚未 `exploited` 的条目，作为新任务**首轮优先续做清单**——这是「增量」而非「重扫」的关键。
- **断点提示**：`last_task_id` 用于 Reporter / 控制台显示「本任务接续目标 <target_id> 历史（共 N 条事实 / M 条威胁）」，便于审计与回放。
- **不继承 `pending/rejected`**：上一次任务未完成的 `pending` 与已排除的 `rejected` 不跨任务注入，避免把半截状态/误报当历史基线；新任务自行重新评估未知面。

#### 4.4.3 PG 侧去重约束：避免目标聚合库无限膨胀

同目标多次任务会产生语义相同的 facts。PG 聚合层须以 `(target_id, category, key)` 唯一约束 + upsert 收敛（详见 §2.8 / §5.1）：

- 冲突时按 **置信度取高**（`confidence` 高者覆盖），保留 `source_task` 历史数组与 `first_seen` / `last_seen` 时间戳。
- 效果：同一目标第 N 次任务，PG `ReconFact` 行数收敛于「该目标去重后的事实全集」，不随任务次数增长。
- 本地 per-task 库**不做**此唯一约束（§2.2 已移除），全量保留本次增量，便于回放与对账。

#### 4.4.4 与现有注入体系的衔接（基线物化进本地库）

> **v2.1 修订（关键澄清）**：续扫任务的 `recon_lookup` 工具查询面**只应是本地 SQLite**（任务进行时禁止触 PG，见 §1 关键约束）。因此 `load_baseline_from_pg` 拉取的 PG 聚合数据**必须物化写回新任务的本地 per-task 库**，而非仅作为上下文注入字符串。

- **物化流程**：`load_baseline_from_pg` 返回后，`ReconStore.seed_from_baseline(baseline)` 将 PG 聚合的 `ReconFact`/`ReconThreat`/关联 `Evidence`（含原文、置信度评分、`first_seen/last_seen`）**写入新建的本地库**，等同一次历史数据的本地重建。
  - 物化时 `task_id` 标为新任务（`source_task` 保留历次任务数组，供审计），`is_baseline=True` 标记区分「历史带入」与「本次增量」，便于 Reporter 区分与回放。
  - 原文证据从 PG 同步时**原样带入**（PG 同步的是本地库原始数据，非脱敏副本，见 §5.4），故续扫任务 `recon_lookup` 能查到上一批证明漏洞存在的响应原文/payload，**无需重打已知**。
- **查询面单一化**：物化后，新任务本地库 = 历史基线 + 本次增量的全集。`recon_lookup` / `build_index_view` **只查本地库**，无需双源 fallback，逻辑与首扫任务完全一致。
- **L0 注入仍保留**：物化的同时，`summary` 仍作为 L0 基线块注入 supervisor 上下文（顶层概览），与物化进库的证据详情互补：L0 给"概览"，`recon_lookup` 给"按需原文"。

**效果**：无论首扫还是续扫，agent 在利用/验证阶段面对的本地库形态完全一致——它**不需要知道数据来自本次还是历史**，也不需要知道 PG 存在。任务进行时零 PG 依赖。

---

## 5. PG 同步层设计（分层、异步，目标态）

> 现状：PG 仅有 `Finding/Artifact/TaskEvent` 等（`pobi_v2/db/models.py`），**无 `ReconSyncLog`**。本节为未来同步层设计，落地前需新建 `ReconSyncLog` 表（alembic 迁移）。

### 5.1 分层同步策略（per-target 聚合层）

> **v2.1 修订**：PG 同步层新增**按 `target_id` 聚合的 `ReconFact` / `ReconThreat` 表**（§2.8），作为跨任务记忆层与 `load_baseline_from_pg` 的数据源（§4.4）。本地 per-task 库是唯一写入入口，PG 聚合层以 upsert 收敛同目标多任务的重复事实。

| 本地数据（per-task） | PG 目标层 | 说明 |
|---|---|---|
| `recon_facts`（全量本次增量） | `ReconFact`（per-target，upsert） | 按 `(target_id, category, key)` 收敛（§2.8）；置信度取高 + `first_seen`/`last_seen` |
| `recon_threat_entries` (verified/exploited) | `Finding` | 正式漏洞，进报表/统计 |
| `recon_threat_entries`（全量） | `ReconThreat`（per-target，upsert） | 跨任务目标维度威胁聚合，供基线拉取 |
| 全量（含 pending/rejected） | `ReconSyncLog` / 归档表（待建） | 仅审计/复盘元数据，不存证据内容 |
| `recon_evidence`（原文+置信度） | `recon_facts_agg`/`recon_threats_agg` 关联证据字段 | 原始同步（密文随本地库形态），**非脱敏副本**（§5.4）；业务出口 `Artifact`/`Finding` 才做合规脱敏 |

**聚合写入要点（P0）**：
- `sync_to_pg` 对 `ReconFact` / `ReconThreat` 一律走 `INSERT ... ON CONFLICT(target_id, category, key) DO UPDATE`（威胁表冲突键为 `(target_id, title_hash)` 或 `(target_id, target_endpoint, category)`，落地择一）。
- 冲突时 `confidence` 取高、`last_seen` 刷新、`source_task` 数组追加；`first_seen` 保留首见时间。
- 本层是 §4.4 增量闭环的**回写端**：任务中断也能在 `status` 变更时实时上 PG，故重发任务拉到的基线包含刚扫到的内容。

### 5.2 同步模式

- **增量同步（实时，P0）**：任务进行中仅同步 `status` 变更事件 + 新 fact 的 upsert（轻量），用于 Web 控制台实时进度**与 §4.4 断点续扫基线**。任务中断也不丢已上 PG 的进度。
- **终态全量快照**：任务 `completed/failed` 时由事件钩子触发一次全量 upsert 同步（钩子目前不存在，需在 `PobiV2EventHooks` 或等价位置新增）。
- 幂等 + 重试 + 失败条目 id 列表记录（扩展 `ReconSyncLog.failed_ids`）。
- 大数量任务支持增量续传，避免每次全量开销。

### 5.3 双向一致性（中长期）

- 现状：SQLite 权威，PG 单向副本。
- 风险：Web 控制台改 Finding 状态无法回写本地，断点续跑会割裂。
- 演进：提供 `reconcile_pg_to_local()` 手动重同步接口 + 一致性校验（P2）。

### 5.4 同步数据形态（原始同步，非脱敏副本）

> **v2.1 澄清（关键）**：PG 聚合层（`recon_facts_agg`/`recon_threats_agg`）同步的是**任务本地库原始数据**——含证据原文、`confidence` 置信度评分、`first_seen/last_seen` 等，**不做脱敏**。PG 不保存脱敏副本，它是本地库按 `target_id` 聚合的镜像，供续扫任务拉取并物化回新本地库（§4.4.4）。

- **本地库的保密责任在本地**：`sensitivity=secret` 证据在本地 SQLite 已是 AES-GCM 密文（密钥仅内存，`.db` 权限 600，见 §2.3/§9.4）。PG 同步的是该密文（或密文引用的 blob 路径），**不在同步链路做二次脱敏**——脱敏只发生在业务合规出口（`Finding` 报表对 `secret` 级遮盖，属 `pobi_v2` 既有报表逻辑，不在本 recon 同步层）。
- **`ReconSyncLog`**：仅记录同步元数据（task_id / target_id / 条数 / failed_ids / 时间戳），不存证据内容，更不存脱敏后内容。
- **合规边界**：若合规要求 PG 不得留存 secret 原文，落地时在 `sync_to_pg` 对 `sensitivity=secret` 证据选择「仅同步 `value` 摘要 + 标记 `has_secret=True`」，**但默认不脱敏**（用户已明确"PG 应同步任务的本地数据库"）。该开关作为可配置项，默认关闭脱敏。

> 原 v2 表 §5.1 中「`recon_evidence` → `Artifact` secret 级脱敏为 `[REDACTED]`」属**业务出口报表**（`Finding`/`Artifact` 既有合规路径），与 recon 聚合层同步本地原始数据**不冲突**：聚合层镜像原始，业务出口按需脱敏。

---

## 6. 落地步骤（含集成测试门禁）

> 下列步骤均为**待开发工作项**，当前代码未实现任何一步。

1. **[DB 模型]** 本地 `sqlite_models.py` 新增 5 张表（facts/threat_entries/evidence/link/fts5，per-task，见 §2.2–§2.6），开启外键，枚举归一化；PG 侧新增 `recon_facts_agg` / `recon_threats_agg` 聚合层（§2.8，alembic 迁移，含 `(target_id, ...)` 唯一约束）。注意：`sqlite_models.py` 当前仅 `CodeChunkSqlite`，需确认与现有 `Base`/连接串兼容。
2. **[Store 封装]** 新增 `recon_store.py`：`ReconStore`（`save_*` / `build_index_view` / `lookup` / `update_status` / `build_reconciliation` / `load_baseline_from_pg` / `sync_to_pg`(upsert 聚合) / 迁移 / checkpoint）。
3. **[侦查落库 — 实时增量旁路]** 在 `ContextEngine.add_*` / `execute_supervisor` summarizer 回调（`executor.py:505-540`）处挂**逐条旁路写入** `ReconStore`（大文本外置、敏感分级、category 归一化）。**禁止**仅在 `threat_model()` return 前一次性 dump——因 `StructuredContext` 有 `_max_executions=50/_max_thoughts=20/_max_log_chars=50000` 内存裁剪，长任务会先丢数据（详见 §0.4）。源为内存 dataclass，需做 schema 映射。
4. **[集成测试门禁 — 不通过禁止进入第 5 步]** 构造 200+ facts / 50+ threats / 100+ evidence 合成数据集，验证：
   - `build_index_view("test login")` 返回 **< 2000 token** 且含相关条目；
   - `lookup(keyword="admin", include_evidence=True)` **P99 < 50ms**；
   - 异常中断后重启，**WAL 恢复无数据丢失**；
   - 旧 schema `.db` **自动迁移成功**；
   - **新增（v2.1）**：连续两次对同 `target_id` 任务，第二次 `load_baseline_from_pg` 能拉到第一次 verified 沉淀，且 PG `recon_facts_agg` 行数不随任务次数线性增长（upsert 收敛）。
5. **[基线预热接驳]** 新任务在 `execute_supervisor` 实例化子 agent **之前**调 `load_baseline_from_pg(target_id)` → 注入 L0 基线块（§4.4），实现断点续扫增量。
6. **[注入改造]** `run_exploitation(threat_model:str)` → `recon_index:str(L0+L1+L2) + recon_lookup`；调用处（`pobi_agent.py` / `deadend_runner.py`）同步改。过渡期开关支持双模式对比（清理所有 prompt 对完整 recon 文本的硬依赖）。
7. **[工具暴露]** `recon_lookup` 注册为 Supervisor 工具（含防护）。
8. **[PG 同步]** 事件钩子挂载分层同步（实时 upsert 聚合 + 终态全量）+ 脱敏；`ReconSyncLog` 新建表 + 扩展 `failed_ids`（需 alembic 迁移）。同步须**实时**（非仅终态），保障任务中断后重发仍能拉到最新基线（§4.4/§5.2）。
9. **[对账报告]** Reporter 调 `build_reconciliation()` 输出对账。

---

## 7. 工程落地与运行时保障（P0/P1）

### 9.1 SQLite 加固清单（P0/P1，目标态）

- ☐ 每次连接 `PRAGMA foreign_keys=ON`。
- ☐ 批量写后 `PRAGMA wal_checkpoint(TRUNCATE)`。
- ☐ `PRAGMA user_version` 增量 DDL 迁移（内嵌，不依赖 meta.json）。
- ☐ 单任务单一连接 + 析构清理 `-wal/-shm`。
- ☐ `update_status` 强制合法流转图谱。
- ☐ `recon_lookup` 单轮上限 + 非法 ID 友好提示。
- ☐ `build_index_view` token 硬上限 + 优先级裁剪。

### 9.2 大文本与故障域（P0/P1）

- ☐ >100KB 外置 `blobs/`，DB 仅存摘要+路径。
- ☐ 单条写入上限校验，防磁盘耗尽 DoS。
- ☐ 阶段性快照（每 N 条证据 / 每轮迭代）：轻量元数据快照，降低崩溃丢失面。

### 9.3 沙箱隔离（P0）

- ☐ `task_id` 目录使用规范化绝对路径 + 越界校验，禁止动态拼接跨任务读库。

### 9.4 敏感数据（P0/P2）

- ☐ `sensitivity` 分级 + secret 级 AES-GCM（密钥仅内存）。
- ☐ `.db` 文件权限 600。
- ☐ PG 同步本地原始数据（默认不脱敏，合规开关可选对 secret 级仅同步摘要，§5.4）。

---

## 8. 待评审决策点（补充提案，供评审会直决）

| # | 决策点 | v2 建议 |
|---|---|---|
| 1 | 索引：动态视图 vs 物化 | ✅ 动态视图 + ReconStore 内 LRU 缓存（key=objective_hash，数据变更 invalidate） |
| 2 | facts 全量 vs 过滤落库 | 👉 默认全量入库 + `is_noise`/`persist` 开关（不硬编码阈值，便于回放） |
| 3 | PG 同步粒度 | 👉 分层：verified/exploited→Finding；pending/rejected→审计归档；增量+全量双模式 |
| 4 | `.db` 生命周期 | 👉 保留 7 天回放→gzip 归档；策略可配（永久/立即清理） |
| 5 | 超大证据外置 | ✅ 实施「摘要入库 + 原始报文外置 blob」（>100KB） |
| 6 | evidence_ids 反范式 | ✅ 新建 `recon_threat_evidence_link` 中间表（范式 + JOIN 性能） |
| 7 | 本地库维度 | ✅ **per-task**（每任务独立 `.db`，故障隔离）；跨任务记忆上移 PG（v2.1，§3.1/§0.3.1） |
| 8 | 续扫基线源 | ✅ 新任务从 PG `load_baseline_from_pg(target_id)` 拉基线，新建本地 per-task 库注入 L0（v2.1，§4.4） |
| 9 | PG 聚合去重 | ✅ `recon_facts_agg`/`recon_threats_agg` 按 `(target_id, ...)` 唯一约束 + upsert 收敛（v2.1，§2.8/§5.1） |

---

## 9. 整改优先级汇总

- **P0（必须修改，否则上线存在稳定性/逻辑风险）**：SQLite 外键约束；状态机合法流转校验；大文本外置防膨胀；`recon_lookup` 单轮上限+非法 ID 友好提示；`build_index_view` token 上限+优先级裁剪；`.db` 权限 600 + secret 加密；路径越界校验。
- **P1（强烈建议，长期隐患）**：增量 PG 同步 + 分层数据分离；连接生命周期 + WAL 临时文件清理；阶段性快照降低崩溃丢失。
- **P2（中长期优化）**：evidence/threat 关联重构（中间表）；敏感数据过滤钩子；PG↔本地一致性校验 + 手动重同步。

---

## 附：与 PLATFORM_STATUS.md 的进度对齐说明

- `PLATFORM_STATUS.md` §4.3 / §5 明确指出「**缺少按 Target 维度的威胁建模信息持久化**」，并将其列为**待启动**设计（指向 `THREAT_MODEL_STORAGE_DESIGN.md`）。本方案即为该方向的延伸细化，**同为待落地状态**，不可读作已完成能力。
- `PLATFORM_STATUS.md` 所述 `ReconSyncLog`、性能告警推送等亦为**待开发**，与本方案 §5 的 PG 同步层设计一致（均未实现）。
- 任何落地前请重新核对 §0.1 代码事实，源码演进后须更新该节再开工。
