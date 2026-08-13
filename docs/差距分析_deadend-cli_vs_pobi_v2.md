# Pobi v2 与 Deadend CLI 差距分析

> 产出时间：2026-08-11 · 产出人：项目负责人 agent
> 对比基准：[deadend-cli README](https://github.com/straylabs-ai/deadend-cli/blob/main/README.md)
> 审阅对象：本仓库 `pobi_v2/` + 内嵌 `pobi_agent/`（源自 deadend-cli 引擎分支）
> 注：本文为能力地图对照，已随代码演进更新（yolo/hacker 模式与审批回调均已落地）。

---

## 一、定位关系：同源衍生，形态不同

Pobi v2 与 Deadend CLI **共享同一套 Agent 引擎内核**（ADaPT 架构 + supervisor-subagent 层级 + 置信度驱动决策）。差异在于产品形态：

| 维度 | Deadend CLI | Pobi v2 |
|------|-------------|---------|
| 产品形态 | 本地命令行工具 | 前后端分离 Web 平台 |
| 交互方式 | 终端 TUI（React/Ink） | 浏览器 SPA（vanilla JS） |
| 部署形态 | 单机二进制（PyOxidizer） | Docker Compose 多服务（api/worker/postgres/redis/nginx） |
| 多用户 | 单用户本地 | 多租户（JWT + 租户隔离） |
| 运行时 | Deno CLI + Python RPC server | FastAPI + ARQ Worker |
| 任务编排 | 一次性命令调用 | 持久化任务队列 + SSE 实时流 + 审批护栏 |

**结论**：引擎能力同源，差距集中在「外围能力」（benchmark 验证、交互模式、白盒分析、工作流编排、可观测性），而非内核决策逻辑。pobi_v2 通过 `engine/deadend_runner.py` 适配层直接驱动原 `DeadEndAgent`，主路径内核与 deadend-cli 等价。

---

## 二、Deadend CLI 关键能力清单 vs Pobi v2 现状

### 2.1 引擎内核（同源，无差距）

| Deadend CLI 能力 | Pobi v2 对应 | 状态 |
|------------------|-------------|------|
| ADaPT 递归规划（PlannerAgent + ADaPTAgent） | `pobi_agent/agents/planner.py` + `engine/deadend_runner.py` 直接驱动 | ✅ 等价 |
| Supervisor-Subagent 层级（6 子 Agent） | `pobi_agent/agents/supervisor_agent.py` + factory | ✅ 等价 |
| 置信度策略（fail<20% / expand 20-60% / refine 60-80% / validate>80%） | `pobi_agent` ValidationGate + JudgeAgentStrategy | ✅ 等价 |
| 反馈驱动迭代（自定义 payload 生成） | `python_interpreter` / `shell` 子 Agent | ✅ 等价 |
| 两阶段执行（recon → exploitation） | `recon_threatmodel_agent.py` → `exploit_web_agent.py` | ✅ 等价 |
| 多 LLM 抽象（LiteLLM） | `pobi_agent` ModelSpec（anthropic/openai/openrouter/gemini/requesty/local） | ✅ 等价 |
| 沙箱化工具（Docker shell / Pyodide python / Playwright HTTP） | `pobi_agent/sandbox/` + `tools/shell.py` + `tools/python_interpreter/` + `tools/browser/` | ✅ 等价 |

### 2.2 外围能力（存在差距）

| Deadend CLI 能力 | Pobi v2 现状 | 差距 |
|------------------|-------------|------|
| **XBOW 基准测试 ~80%（Kimi K2.5，~$122）** | 无 benchmark 结果，无评测流水线 | 🔴 **核心差距**：缺乏可量化的能力验证，无法证明引擎在 Web 平台下仍达原 CLI 水准 |
| **CLI 交互模式（`--mode hacker/yolo`）** | `Task.agent_mode`（hacker/yolo）已实现；yolo 自动批准高危调用，hacker 走审批回调 | ✅ 已对齐（hacker=需人工审批默认 / yolo=自动批准） |
| **Presetup 配置向导** | 仅 `.env` + `pyproject.toml` 配置，无引导式初始化 | 🟡 Web 端首次注册即建租户，但 LLM/scope 配置仍需手动改环境变量 |
| **配置文件体系（`config.json` + `settings.json`）** | `Settings`（pydantic-settings）单一来源 | 🟡 pobi_v2 更规范（类型安全），但缺多模型并存配置（deadend-cli 支持同 config 多 provider 并列） |
| **Codebase 白盒分析（`--codebase`）** | `pobi_agent/code_indexer/` 源码已内嵌，但 `resolve_whitebox_stage(enabled=False)` 默认关闭 | 🟡 能力已具备但未启用，依赖 Playwright + Embedder 后端 |
| **报告生成模板（`/report`）** | `engine/report.py` 支持 Markdown/JSON 导出 | 🟡 基础报告已有，但缺「模板化」（deadend-cli 路线图标注 In Progress） |
| **Plan Mode（执行前预审策略 `/plan`）** | 无；任务创建即入队执行 | 🟡 缺策略预审阶段，用户无法在执行前调整 Agent 规划 |
| **Workflow 自动化（save/replay 攻击链）** | 无；任务无复用机制 | 🟡 缺攻击链持久化与重放 |
| **Context optimization（减少冗余工具调用）** | 无显式优化层 | 🟡 依赖引擎自身，未做 Web 层缓存/去重 |
| **Secrets management** | JWT + `.env`，无 secrets 管理器 | 🟡 基础鉴权在，但无凭据保险库（如目标站点账号密码的托管） |
| **Authentication handling（会话/cookie/auth flow）** | `pobi_agent/auth_resolver/` + `authenticator` 子 Agent | ✅ 等价（内嵌） |
| **WAF bypass** | 无显式 WAF 绕过策略 | 🟡 deadend-cli 路线图标注 Future，双方均未落地 |
| **实时事件流 + 组件健康监控** | SSE 实时流（`/tasks/{id}/stream`）+ `showComponentStatus` | ✅ pobi_v2 有 SSE，但缺「组件健康监控」面板（Docker/Redis/沙箱状态） |
| **多目标编排（interconnected systems）** | 单 Target 单 Task | 🟡 deadend-cli 路线图 Future，双方均未落地 |

---

## 三、差距分级与影响

### 🔴 P0 — 阻断「可信可用」的核心差距

#### G1. 无基准测试验证
- **现状**：deadend-cli 在 XBOW 104 题验证集取得 ~80%（Kimi K2.5），是当前自主渗透 Agent 的事实性能基线。pobi_v2 虽复用同源引擎，但：
  - 无 XBOW 或等价评测跑通记录；
  - 无 API 成本/Token 消耗基线；
  - Web 适配层（`deadend_runner` scope 写入、事件总线、审批回调）可能引入引擎行为偏移，未验证。
- **影响**：无法回答「Web 平台版是否仍达 CLI 版水准」，对外不可量化背书。
- **缩小方向**：
  1. 引入 XBOW validation suite 或自建等价题集；
  2. 在 pobi_v2 环境跑通全量评测，记录通过率 + 成本；
  3. 与 deadend-cli 公开数据对齐，输出基准报告到 `docs/benchmarks/`。

#### G2. 执行模式（yolo/hacker）与审批护栏（已对齐）
- **现状**：deadend-cli 提供 `--mode hacker`（审批）/ `--mode yolo`（全自动）二态。pobi_v2 已实现 `Task.agent_mode`（默认 `hacker`，可选 `yolo`），审批回调链路已修复（回调内 `create_approval_request` + 按独立 `tool_call_id` 等待/决策，避免同任务多次高危调用主键冲突），前端任务创建页支持模式选择。
- **结论**：该差距已收敛，无需再作为阻塞项。

### 🟡 P1 — 产品完整度差距

#### G3. 白盒分析未启用
- **现状**：`pobi_agent/code_indexer/`（SourceCodeIndexer）源码已内嵌，deadend-cli 标注 `--codebase` Coming Soon；pobi_v2 默认 `enabled=False`。
- **影响**：纯黑盒扫描，缺 AST/数据流分析，对逻辑漏洞覆盖不足。
- **缩小方向**：依赖齐备（Playwright + Embedder 后端）后，在任务创建增 `codebase_path` 字段，打通 `resolve_whitebox_stage(enabled=True)`。

#### G4. 缺策略预审（Plan Mode）
- **现状**：deadend-cli 路线图 In Progress `/plan`；pobi_v2 任务创建即入队，用户无法在执行前审阅/调整 Agent 规划。
- **影响**：高成本扫描任务（~$122/次）无法预演策略，成本失控风险。
- **缩小方向**：增「规划阶段」任务状态（`planning` → 用户确认 → `queued`），前端展示 Agent 生成的子目标树供审批。

#### G5. 缺攻击链复用（Workflow save/replay）
- **现状**：deadend-cli 路线图 In Progress；pobi_v2 任务一次性，无跨任务复用。
- **影响**：重复目标/同类漏洞需重跑，成本高。
- **缩小方向**：`Task.template` 字段，支持基于历史任务派生新任务。

#### G6. 缺组件健康监控面板
- **现状**：deadend-cli `showComponentStatus` 展示 Docker/Playwright/RPC 健康；pobi_v2 无等价面板。
- **影响**：用户不知沙箱/Docker 是否就绪，任务失败难定位是环境还是 Agent。
- **缩小方向**：增 `GET /api/v1/health/components` 探测 Docker/Redis/沙箱状态，前端展示。

### 🟢 P2 — 远期对齐（双方均未落地）

- **WAF bypass**：deadend-cli 标注 Future，pobi_v2 无。共同演进。
- **多目标编排**：deadend-cli 标注 Future，pobi_v2 单 Target。共同演进。
- **开源模型 75%+**：deadend-cli 目标 Llama/Qwen，pobi_v2 ModelSpec 已支持 local provider，待评测。

---

## 四、路线建议（对齐 deadend-cli 能力地图）

> 优先级锚定代码现状；下列为已修复项之上的能力补齐。

| 序号 | 任务 | 优先级 | 依赖 | 对齐 deadend-cli |
|------|------|--------|------|------------------|
| C1 | 引入 XBOW 评测子集，跑通基准 + 输出报告 | P0 | 主路径已可用 | benchmark ~80% |
| C2 | 任务模式 `yolo`/`hacker` 切换 + 审批回调（**已完成**） | ✅ 已落地 | — | `--mode` |
| C3 | 组件健康监控面板 | P1 | — | `showComponentStatus` |
| C4 | Plan Mode（规划预审） | P1 | C2 | `/plan` |
| C5 | 白盒分析启用（`codebase_path`） | P1 | Embedder 后端 | `--codebase` |
| C6 | 攻击链复用（Task 模板） | P2 | — | workflow replay |
| C7 | 报告模板化 | P2 | — | `/report` templating |

---

## 五、结论

1. **引擎同源，内核无差距**：pobi_v2 通过 `deadend_runner.py` 直接驱动原 `DeadEndAgent`，ADaPT 架构、置信度策略、子 Agent 层级、沙箱工具与 deadend-cli 等价。
2. **外围能力存在系统性差距**：核心 P0 已收敛为「**无 benchmark 验证**」一项（执行模式 yolo/hacker 与审批回调已落地），其余为产品完整度 P1/P2 差距。
3. **Web 形态带来 deadend-cli 不具备的能力**：多租户、持久化任务队列、SSE 实时流、审计日志、结构化报告导出、agent_mode 策略切换——这些是 pobi_v2 的增量价值，非差距。
4. **建议优先级**：先补 benchmark（C1）达到可量化基线，再推进组件健康监控（C3）、Plan Mode（C4）、白盒分析（C5），即可达到「可信可用的 Web 版 deadend-cli」基线。
