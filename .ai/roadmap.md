# 开发路线图

> 取自 `README.md` 待办、`docs/PROJECT_GOAL.md` §2.4/§2.5。以源码与 docs 为准。

## 已落地（本轮更新确认）
- [x] **子 agent 死循环熔断 + 足迹驱动收敛（2026-08-31）**：修复任务 `535aee8e` requester 死磕 UNION SELECT 被 connection reset 拦截后 50 轮不收敛、30min 熔断 failed 的问题。落地：`pw_send_payload` 实时足迹写 `recon_techniques`（含 endpoint 前缀、成功/失败均记）→ 接通 `was_already_attempted` 防重复 → `is_surface_dead` 死路硬护栏（>=10 失败无成功即 BLOCKED）→ supervisor/requester prompt 注入 "Failed Attempt Footprints" 摘要证据引导转向 → 超时分支补错误描述。用户自行新建任务验证。
- [x] **跨任务数据存储（Recon 双主轴）**：`docs/RECON_LOCAL_STORE_DESIGN.md` 描述，2026-08-19 三阶全合入：
  - 本地 SQLite per-task（`recon_sessions/facts/endpoints/techniques/threats` + `ReconStore`，WAL/AES-GCM 占位/`lookup`/L0-L2 token 预算裁剪），`ContextEngine` 旁路非阻塞写入。
  - **PG 聚合层 per-target**（`recon_facts_agg`/`recon_threats_agg`/`recon_threat_evidence_link`，`pobi_v2/db/recon_models.py`，alembic `0014_recon_agg.py`）：按 `target_id` 跨任务收敛历史沉淀。
  - 闭环：`recon_sync_worker` 订阅 `__recon_sync__` 异步 upsert 到 PG；新任务 `ReconStore.seed_from_pg(target_id)` 预热复用；增量续扫 + 威胁状态机（`suspected→confirmed→exploited→remediated`）+ prompt 显式跳过重复工作。
  - **2026-08-27 修复**：本地库路径统一为任务级单一库 `tasks/<task_id>/<task_id>.db`（去掉 `recon/` 子目录），并修复双库根因（`pobi_agent.py` 原误用 `agents_storage_root` 作 task_root）；`recon_sync_worker` 已在 `main.py` 启动期拉起；`deadend_runner.py` 真实任务分支补注入 `target_id`/`tenant_id`（链路探针 `probe` 走 `probe_runner` 轻量路径，不构造 `DeadEndAgent`，不落库）。
  - 测试：`tests/recon/` 49 passed / 1 skipped（迁移用例需 `POBI_TEST_PG_URL`）。
  - 剩余可选项（非阻塞）：本地↔PG evidence 双向同步、真实 PG e2e、secret 级密钥管理（当前 AES-GCM 占位明文）。

## 进行中
- [ ] **自定义工具添加（下一步）**：在当前工具体系（`pobi_agent/tools/`，含 `tool_wrappers.py` 审批包裹、`models/registry.py` 注册、`context_engine.py`/`architecture.py` 接入点）上，支持用户自定义工具注入到 Agent 工具集。路线草案（待细化）：
  - T1 明确自定义工具的来源形态：用户提供脚本（Python 函数 / CLI 包装）还是配置声明（name + 描述 + 参数 schema + 执行入口）。
  - T2 设计注册入口：扩展 `models/registry.py` 或新增 `custom_tools` 注册表，支持运行时挂载而不改内核代码。
  - T3 复用 `tool_wrappers.py` 的 `ApprovalProvider` 包裹，自定义高危工具同样走审批护栏（fail-closed）。
  - T4 `ContextEngine` / `architecture.py` 在 Agent 装配工具集时并入自定义工具，并按 `agent_mode`（`hacker`/`yolo`）决定默认审批策略。
  - T5 持久化与多租户隔离：自定义工具定义按租户/任务归属存储（PG），不与 `pobi_agent` 内核硬编码工具混淆。
  - T6 前端：任务创建页提供"附加自定义工具"入口，列出可用工具并支持参数预览。
  - 约束：自定义工具执行须经沙箱（DooD `pobi_kali`）或受控边界；须服从授权范围护栏（`ScopePolicy`），越权调用失败关闭。

- [ ] M9 预研：模型按角色分流（payload 走本地无审核模型，规划推理走云端有审核模型），4 步路线：
  - M9-1 `llm/config.py:get_model_spec` 增加 `role` 参数，按 `POBI_V2_MODEL_{ROLE}`/`POBI_V2_LLM_{ROLE}_API_KEY`/`POBI_V2_LLM_{ROLE}_API_BASE` 解析
  - M9-2 引入 `ModelRouter(default, roles)`，`deadend_runner` 注入替代裸 `ModelSpec`
  - M9-3 `pobi_agent/agents/factory.py:AgentRunner` 兼容 `ModelRouter`，按 Agent 名取模型
  - M9-4 标记 payload 生成角色（shell/python_interpreter 绑定 payload），可选补前端"任务可选模型"UI
  - 约束：所有模型仍经 LiteLLM 统一路由与计费；本地 `api_base` 走内网，凭证与云端隔离

## 待办
- [ ] 工程治理（A1–A7，`docs/PROJECT_GOAL.md` §2.4）：分层倒置 / `python_scripts/` 失序 / `logs/` 入版本控制 / `llm/` 孤儿模块 / CORS 不安全 / `main.py:web_app` 分支矛盾 / `routers/system.py` 脆弱写法
- [ ] 扫描内核优化（S1–S6，`docs/PROJECT_GOAL.md` §2.5）：侦查产物结构化与向量化 / 上下文按需检索 / 指纹识别能力 / 漏洞利用工具补全 / Supervisor prompt 去靶场假设 / LLM 决策与工具执行分工
- [ ] **requester 抓页-重抓循环优化（2026-08-28 由任务 `535aee8e` 暴露）**：任务 23.5h 空转，requester 反复执行**同一** `run_python_file` 脚本抓取 DVWA sqli 整页 HTML 达 35 次，直到 iteration 44 才确认注入所需信息（security=low、`GET /vulnerabilities/sqli/` 的 `id` 参数），全程 0 次真实 SQLi 注入、0 findings。
  - 根因：`run_python_file` 输出整页 HTML 超长，`truncate_string` 截断至 20000 token，requester 每次"没看全"表单/认证细节 → 重抓重确认。
  - 方向（待评估）：
    - R1 工具层精炼输出：页面/脚本结果按"关键结构提取"（表单字段、端点、cookie 名、参数）输出，而非整页 HTML 原样透传。
    - R2 提示词引导：要求 requester 一次性提取并**显式落盘**已确认的表单/端点结论（写入 recon/memory），后续直接引用而非重抓。
    - R3 上下文去重：同源大块工具结果在进入模型前做摘要/去重，避免反复把整页 HTML 灌入上下文挤占窗口。
  - 验证：重跑 DVWA 类任务，从任务创建到首次真实注入请求的工具调用轮数应显著下降（目标 < 10 轮）。
- [ ] 白盒代码分析（可选）：`pobi_agent.code_indexer.SourceCodeIndexer`（Playwright/Embedder/RAG），默认关闭，依赖齐备后启用，缺失优雅降级黑盒

## 方案取舍备忘（待固化 constraints.md）
- M9 模型路由需保持 token 用量真实、价格按内置表估算；本地 `ContentPolicyViolationError` 不入 payload Agent
- 生产 CORS 须收敛为具体 origin，解除 `*`+credentials 同用风险

## 阻塞项
- [ ] **BUG：PG 侦察同步 upsert 失败（JSON vs JSONB 操作符不匹配）**
  - 现象：`recon_sync_worker` 异步把本地库事实/威胁 upsert 到 PG 聚合层时失败，PG 聚合表（已通过 `alembic upgrade head` 创建于 2026-08-27）始终为空。
  - 根因：`pobi_v2/db/recon_models.py` 中 `ReconFactAgg.source_tasks` / `ReconThreatAgg.source_tasks` 声明为 `JSON` 类型（`sa.JSON`），但 `pobi_agent/recon/store.py` 的 `upsert_to_pg` 用 `source_tasks.op("||")(stmt.excluded.source_tasks)` 做数组追加——`||` 连接器仅对 `jsonb` 有效，对 `json` 报 `operator does not exist: json || json`。
  - 影响点：`store.py:756`（ReconFactAgg）、`store.py:778`（ReconThreatAgg）。
  - 修复方案（待实施，二选一）：
    - 方案 A（推荐）：将 `recon_models.py` 两处 `source_tasks` 由 `JSON` 改为 `JSONB`，新增 alembic 迁移 `0015_*.py`（`alter_column` type → `JSONB`，PG `USING source_tasks::jsonb`）。模型/同步代码无需改。
    - 方案 B：不动表类型，改 `store.py` 合并逻辑——`source_tasks` 改为 `func.coalesce(ReconFactAgg.source_tasks, '[]'::json) || ...` 之类需 jsonb 的写法仍不可行；彻底绕过 `||`：冲突时以 `stmt.excluded.source_tasks` 整体覆盖（丢失跨任务累计），或用 `postgresql.insert` + `sqlalchemy.sql.expression.type_coerce` 转 jsonb。复杂度高于方案 A。
  - 验证：修复后在 `docker-compose` 容器中 `alembic upgrade head`，重启 `worker`，观察 `recon_facts_agg`/`recon_threats_agg` 随任务落库填充。

- 无明确阻塞（截至 2026-08）。历史监控报告 A–D 的基建阻塞均已修复（事件可观测性 / 认证死循环 / 结构性受阻 / 协作式取消 / AVFS 命名空间 / function-call 序列化）。
