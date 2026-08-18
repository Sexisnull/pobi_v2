# 校验逻辑未读取 per-task 配置文件的问题记录

> 日期：2026-08-17
> 涉及模块：`pobi_agent/agents/components/validation_strategies.py`、`pobi_agent/constants.py`、`pobi_agent/pobi_agent.py`

## 1. 问题（Issue）

`validation_strategies.py` 的校验系统**未优先读取任务级（per-task）校验配置**，与 `constants.py` 注释中规划的 `tasks/<task_id>/validation.<task_id>.yaml` 归口不一致。

用户预期行为：

> 应优先去 `~/.pobi_v2/<task_id>/validation.<task_id>.yaml` 获取校验逻辑；若该文件为空/不存在，则通过 LLM 判断。

实际行为：

- 校验系统只读取**全局**配置文件 `~/.pobi_v2/validation.yaml`（不带 task_id）。
- 全代码库没有任何调用点传入 per-task 校验路径。
- per-task 校验文件 `tasks/<task_id>/validation.<task_id>.yaml` 仅在 `constants.py` 注释里规划，**未接线**。

## 2. 证据（Evidence）

### 2.1 全局路径定义（`pobi_agent/constants.py:63`）
```python
DEADEND_VALIDATION_CONFIG_PATH = ROOT_DEADEND_PATH / "validation.yaml"
# ROOT_DEADEND_PATH = ~/.pobi_v2（可由 POBI_HOME 覆盖）
```

### 2.2 配置加载只回退到全局路径（`validation_strategies.py:121-130`）
```python
If *path* is ``None``, falls back to ``DEADEND_VALIDATION_CONFIG_PATH``.
...
config_path = Path(path) if path else DEADEND_VALIDATION_CONFIG_PATH
```
即：任何未显式传入 `path` 的调用，永远只读全局 `validation.yaml`。

### 2.3 调用点从未传入 per-task 路径（`pobi_agent.py:96,111`）
```python
validation_config_path: str | None = None,   # 默认 None
...
self.validation_config = load_validation_config(validation_config_path)
```
全代码库搜索 `validation_config_path` / `get_task_root() / "validation"` / `validation.{task_id}`，**无一处**构造或传递 per-task 路径（对比 `scope.<task_id>.yaml`、`session_metrics`、`context_engine` 均已通过 `get_task_root()` 落 `tasks/<task_id>/`，但 validation 未接）。

### 2.4 规划归口与实现偏差（`constants.py:28-30`）
注释明确规划：
```
任务级产物统一归口到 TASKS_ROOT = <POBI_HOME>/tasks
按任务号关联产物：tasks/<task_id>/validation.<task_id>.yaml
```
但 `validation_strategies.py` 读取逻辑未实现该归口。

### 2.5 "配置为空则用 LLM" 的真实机制
用户预期"配置为空 → 退化到 LLM"。实际更准确：默认策略链已内建 LLM judge，并非"退化"。
- `load_validation_config` 在文件不存在/为空/解析失败时，返回默认 `ValidationConfig()`，`strategies = [flag, judge]`（`validation_strategies.py:106-111`）。
- `build_validation_gate` 据此构造 `ValidationGate([FlagStrategy(), JudgeAgentStrategy(model)])`（L363）。
- 执行顺序（短路优先）：
  1. `FlagStrategy`（L185）：正则 `FLAG\{...\}` 确定性匹配，零 LLM 成本，命中即 stop。
  2. `JudgeAgentStrategy`（L239）：用 `JudgeAgent` 评估 root_goal 是否满足，含自节流（无新证据则跳过）。

## 3. 修复方案（Fix）

目标：让校验系统**优先读取 per-task 配置**，不存在/为空再回退全局 `validation.yaml`，最终再由默认链（含 LLM judge）兜底。

### 3.1 修改调用点（`pobi_agent.py`，约 L111）
在 `load_validation_config` 调用处，优先构造 per-task 路径（仅当文件存在时才传，否则传 `None` 走全局回退）：

```python
from pobi_agent.storage_context import get_task_root

task_root = get_task_root()
per_task_path = (
    task_root / f"validation.{self.session_id}.yaml"
    if task_root else None
)
self.validation_config = load_validation_config(
    str(per_task_path) if (per_task_path and per_task_path.exists()) else None
)
```

说明：
- `session_id` 即任务号 `task_id`，与现有 `scope.<task_id>.yaml`、`session_metrics` 命名保持一致。
- 文件不存在时传 `None`，`load_validation_config` 自动回退到全局 `DEADEND_VALIDATION_CONFIG_PATH`；文件存在但为空/非法，则走其解析失败分支返回默认链（含 LLM judge）。

### 3.2 （可选）在 `load_validation_config` 内增加 per-task 解析日志
便于排查"究竟读了哪个配置"，建议在 `validation_strategies.py:130` 附近增加：
```python
logger.info("Validation config loaded from: %s", config_path)
```

### 3.3 校验标准（完成后应验证）
- 存在 `tasks/<task_id>/validation.<task_id>.yaml` → 优先使用其内容作为策略链。
- 不存在该文件但存在 `~/.pobi_v2/validation.yaml` → 使用全局配置。
- 两者皆无 → 使用默认链 `[flag, judge]`（flag 确定性 + LLM judge 兜底）。
- 日志能明确打印实际加载的 `config_path`。

## 4. 状态
- [ ] 代码未修改（仅记录）。
- [x] 已实施 3.1（见下方「修复状态」）。
- [ ] 待补充 per-task 校验 YAML 模板示例到 `docs/`。

## 修复状态（已落地）

根因：`pobi_agent.py` 在 `__init__` 阶段就把根级 `validation.yaml` 加载为 `self.validation_config` 并注入 `ValidationGate`/`ReporterAgent`，因此**每个 task 都复用同一份根配置**，task 级 `tasks/<task_id>/validation.<task_id>.yaml` 在验证阶段从未被读取（写而不读）。

修复方式（最小改动、向后兼容）：

1. `validation_strategies.py`
   - 新增 `resolve_validation_config()`：优先读取 `<task_root>/validation.<task_id>.yaml`（task_id = `task_root.name`），文件不存在则回退到根级 `DEADEND_VALIDATION_CONFIG_PATH`，再回退到默认链 `[flag, judge]`。
   - `ValidationGate` 支持 **lazy 模式**（`ValidationGate(model=...)`，策略列表在首次 `check()` 时按解析后的 config 构建并缓存），新增 `validation_metadata()` 暴露当前 task 的 `(validation_type, validation_format)`。
   - 抽出 `_build_strategy_instances()`，供 eager/lazy 两种构造方式复用。
2. `pobi_agent.py`：移出 `__init__` 阶段的 eager 加载，改为 `self.validation_gate = ValidationGate(model=self.model_spec)`（lazy）；reporter 改为在 `executor._run_validation_and_report` 中按 `validation_metadata()` 惰性构建，确保 flag 格式感知的指令生效。
3. `executor.py`：`_run_validation_and_report` 在 `self.reporter` 为 `None` 时，依据 gate 解析出的 per-task 元数据构建 `ReporterAgent`。
4. `scan_workflow.py`：扫描工作流的 `ValidationGate` 同样切换为 lazy 模式，复用 per-task 配置。

验证：四个改动文件 `py_compile` 通过，lint 无告警。`validation_builder.py` 一旦将 `validation.<task_id>.yaml` 写入 `task_root`，验证阶段即可自动生效（前向兼容）。
