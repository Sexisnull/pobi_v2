# 真实目标场景下的校验（Validation）与任务终止问题

> 日期：2026-08-17
> 涉及模块：`pobi_agent/agents/components/validation_strategies.py`、`pobi_agent/core_agent/core_agent.py`、`pobi_agent/agents/architecture.py`、`pobi_agent/pobi_agent.py`

## 1. 问题（Issue）

当前校验系统在 **CTF / 打靶场**场景设计良好（目标 = 拿到 `FLAG{...}`），但在 **真实目标（找漏洞）** 场景下存在以下核心问题：

1. **用户不写 per-task 校验配置时，LLM 如何知道"最终目标达成"？**
2. **若 LLM 永远判定"未达成"，任务会不会无限运行、直到 6 小时自行结束？**
3. 真实目标场景下 `flag` 策略失效、`judge` 对开放目标判定模糊、缺少 wall-clock 熔断。

## 2. 佐证材料（Evidence）

### 2.1 任务不会无限运行——有两层硬上限
- **Supervisor 循环上限**（`core_agent.py:360-362`）：
  ```python
  max_iterations = 50  # Safety limit
  while iteration < max_iterations:
  ```
  supervisor 多轮调子 Agent，最多 50 轮后自然结束（随后由 `ReporterAgent` 出"未达成"报告）。
- **ADaPT 任务树深度上限**（`architecture.py:198`，`pobi_agent.py:95`）：
  ```python
  max_depth: int = 3
  if depth > self.max_depth:
      node = "aborted:max_depth"
  ```
- **没有全局 6 小时 wall-clock 熔断**：搜索 `timeout` / `timedelta` / `3600`，仅有 sandbox 命令级超时（`SANDBOX_COMMAND_TIMEOUT_SECONDS`）与 LLM 重试超时，无任务级 wall-clock 上限。"6 小时"为经验估算，非代码硬编码。

### 2.2 用户不写配置时，judge 靠 `root_goal` 判定
- `JudgeAgentStrategy.check`（`validation_strategies.py:289-308`）将下发的 `root_goal` 直接注入 prompt：
  ```
  ## 目标
  {root_goal}
  ...请依据执行轨迹，判断下列目标是否已达成...
  ```
- 同时喂入整个执行轨迹（`detailed_summary` / `proofs` / `subagent_log` / `supervisor_history` / `context`）。
- 输出 `_JudgeOutput`：`valid` / `confidence_score` / `critique` / `validation_token`。
- 结论：**`root_goal` 就是默认"目标验证"标尺**，per-task YAML 仅提供确定性 shortcut（如 `flag` 正则），非必需。

### 2.3 默认策略链对真实目标几乎无效
- 默认 `ValidationConfig.strategies = [flag, judge]`（`validation_strategies.py:106-111`）。
- `FlagStrategy`（L185）仅匹配 `FLAG\{...\}` 正则——真实漏洞场景无 flag，永远不命中。
- 实际只剩 `JudgeAgentStrategy`（LLM）兜底，而其对"找到漏洞"等开放目标判定标准模糊。

### 2.4 judge 自节流可能掩盖"未达成"
- `JudgeAgentStrategy.check`（L283-287）：若 context 哈希未变，直接返回 `stop=False` 跳过，**不调 LLM、也不停**。
  ```python
  current_hash = hash(context)
  if current_hash == self._last_context_hash:
      return ValidationVerdict(stop=False, confidence=0.0)
  ```
- 含义：若子 Agent 多轮未产出新证据，judge 静默放行，靠 supervisor 循环耗尽 50 轮自然结束。

### 2.5 预设已区分场景但未覆盖"真实漏洞"
- `PRESETS`（`validation_strategies.py:409-413`）：
  ```python
  "flag":  ["flag"],
  "judge": ["judge"],
  "ctf":   ["flag", "judge"],
  "recon": ["judge"],
  ```
- 已有 `recon` 预设（仅 judge），但缺"真实漏洞利用/确认"专属预设，judge 指令未区分 CTF 与真实目标。

## 3. 解决方案（Fix）

按侵入性从小到大，分三层建议：

### A. 根目标必须含可验证成功标准（无需改代码）
- 启动任务时强制 `root_goal` 写明**可验证的达成条件**（如"确认存在 CVE-xxx / 拿到 RCE / 读到 /etc/passwd 内容"），而非模糊描述（如"找漏洞"）。
- 前端/CLI 对 `root_goal` 做最小校验：缺失具体成功判据时提示用户补全。
- 依据：judge 以 `root_goal` 为唯一标尺（见 2.2），写清楚即可工作。

### B. 新增真实目标预设 + 区分 judge 指令（小改）
- 在 `PRESETS`（`validation_strategies.py:409`）增加：
  ```python
  "vuln": ["judge"],
  ```
- `judge.instructions.jinja2` 增加分支：当 `validation_type == "vuln"` 时，要求 judge 必须返回**已确认漏洞列表 + POC**，且 `valid=true` 须附可复现证据；当 `validation_type == "flag"` 时维持 CTF 语义。
- 在 `pobi_agent.py` 下发 `validation_type` 时，按任务类型（CTF / 真实）选择 `flag` 或 `vuln`。

### C. 新增 wall-clock 熔断（中改，推荐）
- 在 `executor.py` 的 exploitation / threat_model 主循环加 wall-clock 上限，超时强制发 `ValidationStopEvent`（以"超时未达成"收尾，而非静默跑满）：
  ```python
  import time
  MAX_RUNTIME_SECONDS = float(os.getenv("POBI_MAX_RUNTIME_SECONDS", "21600"))  # 默认 6h
  start = time.monotonic()
  ...
  if time.monotonic() - start > MAX_RUNTIME_SECONDS:
      # 强制 stop：输出部分报告，标记 confidence 为当前最高
      emit_validation_stop(timeout=True)
      break
  ```
- 上限通过环境变量 `POBI_MAX_RUNTIME_SECONDS` 可调，默认 6 小时，与现有经验值对齐。

### D. 增强 judge 结构化证据（可选）
- 扩展 `_JudgeOutput`（`validation_strategies.py:346`）增加 `confirmed_vulns: list[str]` 字段，要求 judge 在 `valid=true` 时列出具体漏洞与 POC，避免模糊判定。

## 4. 结论摘要

- **不会无限跑**：`max_iterations=50`（supervisor）+ `max_depth=3`（ADaPT）构成硬上限，任务会自然结束，无 6 小时 wall-clock 熔断。
- **用户不写配置也能工作**：judge 默认用 `root_goal` 当标尺，per-task YAML 仅为确定性 shortcut。
- **真实目标场景有缺陷**：flag 失效、judge 模糊、无 wall-clock 熔断、易跑满 50 轮。
- **建议落地顺序**：A（文档/前端）→ B（预设+指令区分）→ C（wall-clock 熔断）。

## 5. 状态
- [x] 验证策略配置下沉到任务级（本问题修复，见下方「修复状态」）。
- [ ] 待实施 A：前端 `root_goal` 校验提示。
- [ ] 待实施 B：`PRESETS` 加 `vuln` + `judge.instructions.jinja2` 分支。
- [ ] 待实施 C：`executor.py` 加 `POBI_MAX_RUNTIME_SECONDS` 熔断。
- [ ] 待实施 D：`_JudgeOutput` 增 `confirmed_vulns`。

## 6. 修复状态：验证策略配置下沉到任务级

### 问题
验证策略（flag 正则 / 验证格式 / 信心阈值带 / 树深度）此前配置在**授权目标级**：
- `targets` 表含 `flag_regex`/`validation_format`/`confidence_threshold`/`max_tree_depth`；
- 前端「新建授权目标」表单展示验证策略区块，而「新建任务」表单无验证策略配置；
- 运行时 `_write_validation_config` 从 Target 读取，任务无法独立配置「怎样才算找到漏洞」。

### 改动
1. **`Task` 新增任务级验证策略字段**（`flag_regex`/`validation_format`/`confidence_threshold`/`max_tree_depth`，可空，None=继承授权目标配置/使用默认）+ 迁移 `0012_task_validation.py`。
2. **`_write_validation_config(task, target, task_id)`** 任务级优先，回退授权目标级，再回退默认值。
3. **前端**：授权目标表单/详情移除验证策略区块；新建任务表单新增验证策略区块（可留空继承默认），草稿与提交 body 均透传。
4. `schemas/task.py` 的 `TaskCreate`/`TaskUpdate`/`TaskRead` 透传新字段；`routers` 因 `Task(**data.model_dump())` 自动承接。

### 收益
- 同一授权目标的不同任务可配置不同验证策略（CTF 用 flag、真实目标用 judge/vuln），为方案 B「按任务类型选择验证策略」提供配置前置。
- 授权目标级字段保留作为默认值来源，存量目标配置不失效（benchmark 仍可用目标级 flag 正则）。

### 待办
- 方案 B 落地时可把 `validation_type` 决策迁移到任务级（如任务新增 `preset` 字段），当前仍由 `_write_validation_config` 按 flag 有无推导。
