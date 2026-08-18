# 威胁建模信息按 Target 维度存储设计

> 文档性质：数据建模设计稿（本轮仅产出设计，不修改业务代码、不执行迁移、不提交）。
> 目标：将任务执行过程中发现的**接口、请求数据、响应数据、侦察指纹**按**授权目标（Target）**维度持久化到本地数据库；当同一 Target 的任务被中断/取消后重新运行时，可直接拉取已缓存信息，**跳过重复侦察，减少扫描时间**。
> 用户决策：存储粒度=按 Target；落库内容=完整请求/响应（含 cookie/header，授权靶场场景可接受）；本轮=文档+设计，编码另排期。

---

## 0. 现状：项目记忆如何储存（已落地机制，重要）

在讨论"按 Target 落库"之前，必须先说明项目**当前已经存在**的记忆储存机制（`MemoryAgent` + AVFS）。本节基于代码静态核查，是后续规划的衔接基线。

### 0.1 MemoryAgent 是什么

`agents/generic_agents/memory_agent.py` 是一个**辅助型子 Agent**，专门负责读写**持久化记忆工作区**。它只有 4 个 tool（全部来自 `tools/avfs/`，即 Agent Virtual File System）：

| Tool | 真实实现 | 作用 |
|---|---|---|
| `list_memory_files` | `avfs/list.py:225` | 列出 memory 命名空间文件 |
| `read_memory_file` | `avfs/read.py:195` | 读 memory 文件（支持行范围/字符上限） |
| `write_memory_file` | `avfs/write.py:78` | **写** memory 文件（可 append） |
| `grep_memory_files` | `avfs/read.py:207` | 在 memory 文件里 ripgrep 搜索 |

这 4 个 tool 全部打 `workspace="memory"`（`avfs` 的 `memory` 命名空间），与 `python_interpreter` 的 `workspace` 命名空间**隔离**。

### 0.2 写到哪里（真实落盘路径）

`memory_workspace_root` 由 `_prepare_memory_workspace`（`pobi_agent.py:268`）决定，真实路径为：

```
{agents_storage_root}/{local_agent_id}/{embedding_session_id}/memory/
```

其中：

- `agents_storage_root`：平台层注入为 **`TASKS_ROOT/<task_id>/agent`**（`constants.py` 目录契约；`deadend_runner.py:359`）。
- `embedding_session_id`：`embedding_session_id or session_id`（`pobi_agent.py:281`、`deadend_runner.py:281`）。
- `session_id`：**等于 `task_id`**（`deadend_runner.py:360-361`，注释 L15 明确 `session_id == task_id`）。
- `local_agent_id`（即路径里的 `agent_id`）：**机器级稳定指纹**，来自 `Config.get_local_agent_id()`（`settings.py:411`），首次运行 `uuid4()` 后持久化进 `config.json`，**跨所有任务不变**。

展开即：

```
~/.pobi_v2/tasks/<task_id>/agent/<local_agent_id>/<task_id>/memory/
```

文件**真实落盘**（`avfs.resolve` 映射虚拟路径到磁盘，`avfs.py:110-114`），如 `memory/summaries/*.md`。

> 冗余说明：`task_id` 在路径中出现了**两次**（外层 `tasks/<task_id>/` 父目录 + 内层 `session_id` 子目录），原因是平台层 `TASKS_ROOT` 归口与内核 AVFS `session_id` 约定两套命名叠加。`local_agent_id` 不参与任务区分，仅保证"同一机器目录布局一致"（`settings.py:415`）。

### 0.3 是否被前端展示？

**否。** `web/` 仅有报告查看接口（`/api/v1/tasks/{id}/report/markdown`、`/report/json`，`app.js:1789`）。搜索前端与 `pobi_v2/` 路由，**无任何暴露 `memory/` 目录或 AVFS 文件的端点**。用户只能看最终报告，看不到原始 memory 文件。

### 0.4 是否供二次扫描复用？

**落盘持久，但默认不自动跨任务复用：**

- memory 文件按 `session_id`（= `task_id`）隔离，**每次新扫描 = 新 task_id = 新 memory 目录**。
- `_populate_memory_context`（`pobi_agent.py:282`）是**启动前把"自己的" memory 读进上下文**，不是读历史任务。
- 技术上可复用（文件就在 `tasks/` 下），但**无"跨任务记忆检索"自动机制**，需手动拷贝或改接线。

### 0.5 与本节设计的关系（衔接点）

| 维度 | 当前 MemoryAgent (AVFS) | 本设计稿拟新增 (Target 表) |
|---|---|---|
| 隔离粒度 | 按 `task_id`（每次扫描） | 按 `target_id`（同一目标跨扫描） |
| 介质 | 本地磁盘文件（`tasks/.../memory/`） | SQL 表（多租户） |
| 内容 | agent 自由书写的笔记/摘要 | 结构化接口/请求响应/指纹 |
| 前端可见 | 否 | 拟提供只读 API（§7） |
| 重跑复用 | 默认不自动 | 目标是核心能力 |

**结论**：现有 MemoryAgent 是"**任务级、非结构化、不可见、不自动复用**"的 agent 笔记本；本设计稿要补的是"**目标级、结构化、前端可见、可重跑复用**"的侦察资产库。两者是**互补**而非替代——规划阶段需明确边界，避免重复建设（见 §5.4）。

---

## 1. 现状缺口

| 现有表 | 存什么 | 缺什么 |
|---|---|---|
| `Target` | 目标 URL/范围 | 无「该目标已发现哪些接口/资产」 |
| `TaskEvent` | 任务运行轨迹（事件流） | 非结构化、按 task 维度、难以按 target 聚合复用 |
| `Finding` | 漏洞证据 | 仅漏洞，不含正常接口资产；且仅成功落库（见 `PLATFORM_STATUS.md` §4.2） |
| `Artifact` | 产物文件 | 同上，仅成功落库 |

**核心缺口**：没有「按 Target 维度的侦察资产/接口/请求响应样本」结构化表。因此重跑同一授权站点无法复用历史接口/请求数据，必须重新扫描。

---

## 2. 内核事件探查结论（采集可行性，重要）

经对 `pobi_agent` 内核与 `event_bus.py` 的静态核查，关键事实：

- **网络层无专用 HTTP 事件**：`EventHooks` Protocol 仅有通用的 `emit_tool_call_start/end`，**没有** `emit_http_request/response`。HTTP 明细只能通过 `tool_call_end` 间接获得。
- **`tool_call_end.payload.result` 是纯文本字符串**（强制 `str` 类型），不是结构化对象，无法直接 `result["status_code"]`。
  - 主力 HTTP 工具 `pw_send_payload` 返回 `str(truncated_responses)`，形如 `['HTTP/1.1 200 OK\r\nContent-Type: ...\r\n\r\n<body>', ...]`。
  - 请求方法/URL/请求头/请求体在 `tool_call_start.payload.args` 的 `raw_request` 文本中。
- **`browser_run_steps`** 返回 dict（success/error/final_url/page_title/steps_run），含 `final_url` 但无 HTTP 明细。
- `RequesterAgent` 经 `call_requester_agent` 委托调用，**不经过 `tool_call_end`**，其返回被转成普通字符串回传 supervisor，无独立事件。

**采集策略（据此设计）**：
1. **近期方案（零内核改动）**：在 `event_bus.py` 的 `persist_event_worker` 中，对 `tool_name=="pw_send_payload"` 的 `tool_call_end`/`tool_call_start` 事件，用 **HTTP 报文正则解析** 抽取 method/url/headers/status_code/body，落库到 Target 表。
2. **中期增强（建议内核改动）**：新增 `EventHooks.emit_http_transaction`，在 `PlaywrightRequester._send_request`/`_format_response` 处发射结构化请求/响应（Pydantic 模型），使采集无需正则解析。本设计稿以方案 1 为默认，方案 2 列作后续增强。

---

## 3. 新增数据模型（按 Target，多租户隔离）

遵循 `pobi_v2/db/models.py` 既有约定：所有表含 `tenant_id`（FK `tenants.id`）+ 索引；UUID 主键；`DateTime(timezone=True)` 时间戳用 `utcnow()`。

### 3.1 `target_assets` —— 接口/资产清单

```python
class TargetAsset(Base):
    __tablename__ = "target_assets"
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    target_id: Mapped[UUID] = mapped_column(ForeignKey("targets.id"), index=True)
    method: Mapped[str] = mapped_column(String(16))          # GET/POST/...
    path: Mapped[str] = mapped_column(String(2048))          # 归一化路径（去 query）
    source_task_id: Mapped[UUID | None] = mapped_column(ForeignKey("tasks.id"), nullable=True)
    tech_stack: Mapped[list[str]] = mapped_column(JSON, default=list)  # 指纹：PHP/Apache/DVWA...
    discovered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    # 唯一约束建议：(tenant_id, target_id, method, path) 避免重复
```

### 3.2 `target_requests` —— 完整请求/响应样本

```python
class TargetRequest(Base):
    __tablename__ = "target_requests"
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    asset_id: Mapped[UUID] = mapped_column(ForeignKey("target_assets.id"), index=True)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    target_id: Mapped[UUID] = mapped_column(ForeignKey("targets.id"), index=True)
    task_id: Mapped[UUID | None] = mapped_column(ForeignKey("tasks.id"), nullable=True)
    method: Mapped[str] = mapped_column(String(16))
    url: Mapped[str] = mapped_column(String(2048))
    request_headers: Mapped[dict] = mapped_column(JSON, default=dict)   # 完整落库（含 cookie）
    request_body: Mapped[str | None] = mapped_column(Text, nullable=True)
    status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    response_headers: Mapped[dict] = mapped_column(JSON, default=dict)
    response_body: Mapped[str | None] = mapped_column(Text, nullable=True)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
```

> 说明：用户已确认**完整落库**（含 cookie/header）。风险缓释：本表仅存授权靶场数据，且随 `target_id`/`tenant_id` 隔离；后续如需降风险可加「凭据脱敏」开关，本轮不强制。

### 3.3 `target_recon` —— 侦察上下文摘要（重跑注入源）

```python
class TargetRecon(Base):
    __tablename__ = "target_recon"
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    target_id: Mapped[UUID] = mapped_column(ForeignKey("targets.id"), unique=True, index=True)
    summary: Mapped[str] = mapped_column(Text)               # 攻击面自然语言摘要
    auth_surface: Mapped[dict] = mapped_column(JSON, default=dict)  # 登录点/CSRF/认证方式
    endpoints: Mapped[list] = mapped_column(JSON, default=list)      # 接口清单快照
    tech_stack: Mapped[list] = mapped_column(JSON, default=list)
    coverage_pct: Mapped[int] = mapped_column(Integer, default=0)    # 侦察完成度，用于判断可否复用
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)
```

---

## 4. 采集点设计（按 §2 采集策略）

### 4.1 采集位置

在 `pobi_v2/engine/event_bus.py` 的 `persist_event_worker`（已有 TaskEvent 落库处）增加分支：

- `tool_call_start` 且 `tool_name == "pw_send_payload"` → 解析 `payload.args` 中的 `raw_request` 文本，暂存「请求侧」到内存缓冲（key=tool_call_id）。
- `tool_call_end` 且 `tool_name == "pw_send_payload"` → 解析 `payload.result` 文本得到响应侧，与缓冲合并，组装一条 `TargetRequest` + 去重写入 `TargetAsset`（按 method+path）。
- 需要 `target_id`：事件需携带 target 上下文。当前 `PobiV2EventHooks` 由 executor 实例化，executor 已知 `target_id`（来自 task），应在构造 hooks 时注入 `target_id`/`tenant_id`，随事件透传。

### 4.2 HTTP 报文解析（正则草案）

```
请求行:   ^(?P<method>GET|POST|...)\s+(?P<url>\S+)\s+HTTP
状态行:   ^HTTP/1\.1\s+(?P<status>\d{3})
头/体分隔: \r\n\r\n
```
解析失败时记录 `status_code=None`，不影响主流程（采集旁路、异常吞掉并记日志，绝不阻断任务）。

### 4.3 解析型采集的局限与增强

- 纯文本解析可能丢失编码/分块细节；建议中期推动内核新增 `emit_http_transaction`（结构化 Pydantic），届时采集改为直接读取字段，无需正则。
- `RequesterAgent` 路径目前不发光，若需覆盖该路径的请求/响应，应在 `executor.call_requester_agent` 返回处补一发事件。

---

## 5. 重跑复用逻辑（减少扫描时间）

### 5.1 读取时机

在 `executor._run_task_body` 启动阶段（现有读 memory 之后），新增：

```python
recon = await get_target_recon(target_id, tenant_id)
if recon and recon.coverage_pct >= REUSE_THRESHOLD:   # 如 >= 60
    context = build_reuse_context(recon, assets, requests)
    # 注入给内核：经 deadend_runner / scan_workflow 的系统提示或初始化上下文
    await inject_reuse_context(context)
    skip_phases = ["recon_basic", "endpoint_enum"]   # 跳过重复侦察
```

### 5.2 复用内容

- `target_assets`：已知接口清单 → 直接作为扫描目标，免去目录爆破/爬虫。
- `target_recon.auth_surface`：已知登录点/CSRF 字段 → 免去认证方式探测（直接规避 DVWA 类死循环，见 `PLATFORM_STATUS.md` §4.1）。
- `target_requests`：已知请求/响应样本 → 作为基线，只验证差异/新端点。

### 5.3 增量更新

- 复用不等于只读：重跑中**新发现**的接口/请求继续写入同 `target_id` 表，逐步提升 `coverage_pct`。
- 中断/取消的任务：因其事件已旁路落库（§4 采集与成功分支解耦），重跑时可立即拉取**上一次已收集的部分资产**，无需从头扫描。

### 5.4 与现有 MemoryAgent（AVFS）的衔接边界

为避免与 §0 已落地的 MemoryAgent 重复建设，明确分工：

- **目标级结构化资产**（接口清单/请求响应/指纹）→ 落 `target_assets` / `target_requests` / `target_recon`（本设计），按 `target_id` 跨扫描复用、前端可见。
- **任务级非结构化笔记**（agent 中间推理、摘要、临时草稿）→ 仍走 MemoryAgent/AVFS（§0），按 `task_id` 隔离，不进入本次 Target 表。
- **重跑注入源唯一**：重跑跳过重复侦察只认 `target_recon`（§5.1），**不读**历史任务的 AVFS memory（默认不可见且非结构化，见 §0.4）。
- **可选桥接**（非必须）：若希望把历史任务 memory 中的关键结论沉淀为 `target_recon.summary`，可新增一次性离线脚本解析 `tasks/<task_id>/agent/.../memory/`，但不在本轮范围。

---

## 6. Alembic 迁移占位（本轮不执行）

建议新增迁移文件：`alembic/versions/0008_target_threat_model.py`

责任：
1. 创建 `target_assets`、`target_requests`、`target_recon` 三表。
2. 建外键：`tenant_id→tenants`、`target_id→targets`、`asset_id→target_assets`、`source_task_id/task_id→tasks`。
3. 索引：`(tenant_id, target_id)`、`(target_id, method, path)` 唯一约束、`target_recon.target_id` 唯一。
4. 不回填历史数据（历史 TaskEvent 文本可后续离线解析填充，非必须）。

> **本轮不运行 `alembic upgrade`**，落地以编码阶段执行为准。

---

## 8. 复核状态（2026-08-18）

- 本文为**设计稿，尚未落地**（三表与重跑注入仍待实现，见 `EVOLUTION_ROADMAP.md` 阶段 7）。
- §0 描述的 `MemoryAgent`/AVFS 机制真实有效；§4 采集策略仍准确。
- 迁移占位版本号 `0008_target_threat_model` 仅为占位，**实际 alembic 已演进到 `0012_task_validation`**（任务级验证字段），本设计落地时需用新的后续版本号，避免冲突。

---

## 7. 接口层扩展（设计描述，不实现）

`pobi_v2/routers/persistence.py` 可新增只读查询（多租户隔离）：
- `GET /api/v1/persistence/targets/{target_id}/assets` —— 接口资产清单
- `GET /api/v1/persistence/targets/{target_id}/requests` —— 请求/响应样本
- `GET /api/v1/persistence/targets/{target_id}/recon` —— 侦察摘要

---

## 8. 落地步骤建议（排期，非本轮）

1. 在 `models.py` 增加三表；生成 `0008` 迁移并 `alembic upgrade`。
2. `event_bus.persist_event_worker` 增加 `pw_send_payload` 解析落库分支；executor 注入 `target_id` 到 hooks。
3. `executor._run_task_body` 增加重跑复用读取与阶段跳过。
4. （可选）推动内核 `emit_http_transaction` 结构化事件，替换正则解析。
5. `persistence.py` 增加 Target 级只读查询接口。
6. 验证：跑一次 DVWA 任务 → 中断 → 重跑 → 确认从 `target_assets` 拉取、跳过重复侦察、耗时下降。
