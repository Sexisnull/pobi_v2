# 开发路线图

> 取自 `README.md` 待办、`docs/PROJECT_GOAL.md` §2.4/§2.5。以源码与 docs 为准。

## 已落地（本轮更新确认）
- [x] **跨任务数据存储（Recon 双主轴）**：`docs/RECON_LOCAL_STORE_DESIGN.md` 描述，2026-08-19 三阶全合入：
  - 本地 SQLite per-task（`recon_sessions/facts/endpoints/techniques/threats` + `ReconStore`，WAL/AES-GCM 占位/`lookup`/L0-L2 token 预算裁剪），`ContextEngine` 旁路非阻塞写入。
  - **PG 聚合层 per-target**（`recon_facts_agg`/`recon_threats_agg`/`recon_threat_evidence_link`，`pobi_v2/db/recon_models.py`，alembic `0014_recon_agg.py`）：按 `target_id` 跨任务收敛历史沉淀。
  - 闭环：`recon_sync_worker` 订阅 `__recon_sync__` 异步 upsert 到 PG；新任务 `ReconStore.seed_from_pg(target_id)` 预热复用；增量续扫 + 威胁状态机（`suspected→confirmed→exploited→remediated`）+ prompt 显式跳过重复工作。
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
- [ ] 白盒代码分析（可选）：`pobi_agent.code_indexer.SourceCodeIndexer`（Playwright/Embedder/RAG），默认关闭，依赖齐备后启用，缺失优雅降级黑盒

## 方案取舍备忘（待固化 constraints.md）
- M9 模型路由需保持 token 用量真实、价格按内置表估算；本地 `ContentPolicyViolationError` 不入 payload Agent
- 生产 CORS 须收敛为具体 origin，解除 `*`+credentials 同用风险

## 阻塞项
- 无明确阻塞（截至 2026-08）。历史监控报告 A–D 的基建阻塞均已修复（事件可观测性 / 认证死循环 / 结构性受阻 / 协作式取消 / AVFS 命名空间 / function-call 序列化）。
