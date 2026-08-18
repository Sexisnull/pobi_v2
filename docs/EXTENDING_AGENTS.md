# 扩展指南：新增 Agent / 提示词 / 工具调用

> 适用版本：pobi_v2（代码快照 2026-08）
> 目标读者：需要在现有多智能体架构中扩展能力的开发者

本文档说明三种最常见的扩展方式，并给出可复用的代码骨架。所有片段均取自当前代码库，路径与签名真实有效。

---

## 0. 架构速览（先读这段）

任务执行的智能体层级（自顶向下）：

```
DeadEndAgent（总控 / 每任务一个新实例）
└── AgentExecutor.execute_supervisor
    └── SupervisorAgent（路由大模型，单轮决策调哪个子 Agent）
        ├── requester_agent            (RequesterDeps)
        ├── auth_resolver              (RequesterDeps，替代旧 authenticator_agent；多步登录/认证画像见 docs/PROJECT_GOAL.md)
        ├── shell_agent                (WebappreconDeps ← 走 Kali 容器)
        ├── python_interpreter_agent   (MemoryWorkspaceDeps)
        ├── webapp_analyzer_agent      (RequesterDeps)
        └── memory_agent               (MemoryWorkspaceDeps)
```

关键点：

- **Supervisor 不是硬编码 pipeline**，而是 LLM 多轮循环（`CoreAgent._run_impl` 的 `while iteration < max_iterations`），每轮由模型决定调用哪个子 Agent 的 tool。
- **子 Agent 自己也是 `CoreAgent` 循环**，可多轮调用其内部的原子 tool。
- **Deps（依赖）是"能力容器"**，与 Agent 解耦。Agent 自带 `target` / `requires_approval` 等字段；Deps 装外部能力（RAG、shell_runner、内存工作区等）。
- **提示词全部来自 `pobi_prompts/` 的 Jinja2 模板**，由 `render_agent_instructions` / `render_tool_description` 渲染，不硬编码在 Python 里。

三种扩展方式的选择：

| 你想做的事 | 走哪条路 |
|---|---|
| 新增一类"会自主推理的专用能力"（如独立指纹识别 Agent） | 新增 Agent（第 1 节） |
| 给现有 Agent 的提示词补充策略/约束/工具清单 | 改提示词模板（第 2 节） |
| 新增一个原子能力（函数 / 调 Kali 命令 / 调外部脚本） | 定义工具（第 3 节） |
| 让现有子 Agent 多一个能力（不新增 Agent） | 定义工具 + 挂到现有 Agent（第 3 节 + 3.4） |

---

## 1. 如何新增一个 Agent

以"指纹识别 Agent（finder）"为例，完整流程如下。

### 1.1 选 / 定义 Deps 类型

Deps 定义在 `pobi_agent/utils/structures.py`。现有类型：

- `RequesterDeps`（L127）：embedder_client / rag / target / agent_id / session_id / proxy_url / memory_*
- `WebappreconDeps`（L153）：在 RequesterDeps 基础上多了 `shell_runner`（命令执行句柄）
- `MemoryWorkspaceDeps`（L179）：session_id / memory_workspace_root / memory_context
- `ShellDeps`（L108）、`RagDeps`（L187）、`TargetDeps`（L242）

若 finder 需"发请求 + 调 Kali"，复用 `WebappreconDeps` 最合适（含 `shell_runner`）。若纯 RAG，用 `RequesterDeps`。

如确需全新 Deps，在 `structures.py` 新增一个 `@dataclass`，并在 `SupervisorDeps`（`executor.py:95`）里加对应字段。

### 1.2 写工具实现（Agent 内部的原子能力）

在 `pobi_agent/tools/finder/__init__.py` 与 `finder.py` 实现。若底层走 Kali，用 `ctx.deps.shell_runner.run_command(...)`（见第 3.3 节）。

### 1.3 写提示词模板

- `pobi_prompts/finder.instructions.jinja2`：Agent 的人设、策略、输出格式。
- 模板中 `{% for tool_name, tool_description in tools.items() %}`（参考 `shell.instructions.jinja2:182`）会展开该 Agent 拥有的 tool 描述。
- 可 `{% include '_shared/_anti_fabrication.jinja2' %}` 等共享片段。

### 1.4 新建 Agent 类

参照 `pobi_agent/agents/generic_agents/webapp_analyzer_agent.py`：

```python
# pobi_agent/agents/generic_agents/finder_agent.py
from typing import Any
from pydantic_ai import Tool, DeferredToolRequests, DeferredToolResults
from pydantic_ai.usage import RunUsage, UsageLimits
from pobi_agent.config.settings import ModelSpec
from pobi_agent.agents.factory import AgentRunner, AgentOutput
from pobi_agent.tools import finder
from pobi_prompts import render_agent_instructions, render_tool_description


class FinderAgent(AgentRunner):
    def __init__(self, model: ModelSpec, deps_type: Any | None):
        tools_metadata = {
            "finder": render_tool_description("finder"),
        }
        self.instructions = render_agent_instructions(
            agent_name="finder",
            tools=tools_metadata,
        )
        super().__init__(
            name="finder",
            model=model,
            instructions=self.instructions,
            deps_type=deps_type,
            output_type=[AgentOutput, DeferredToolRequests],
            tools=[Tool(finder)],
        )

    async def run(self, prompt, deps, message_history,
                  usage: RunUsage | None, usage_limits: UsageLimits | None,
                  deferred_tool_results: DeferredToolResults | None = None,
                  *args, **kwargs):
        return await super().run(
            prompt=prompt, deps=deps, message_history=message_history,
            usage=usage, usage_limits=usage_limits,
            deferred_tool_results=deferred_tool_results,
        )
```

### 1.5 注册到 supervisor（executor.py）

需改三处：

**(a) 实例化 Agent**（`execute_supervisor` 内，L397 起区域）：
```python
from pobi_agent.agents.generic_agents.finder_agent import FinderAgent
# ...
finder_agent = FinderAgent(
    model=self.model,
    deps_type=WebappreconDeps,
)
```

**(b) 扩展 `SupervisorDeps`**（`executor.py:95`）：
```python
finder_agent: FinderAgent | None = None
finder_deps: WebappreconDeps | None = None
```

**(c) 构建 supervisor_deps 时传入**（`executor.py:443` 区域）：
```python
supervisor_deps = SupervisorDeps(
    # ... 已有字段 ...
    finder_agent=finder_agent,
    finder_deps=self.finder_deps,   # 需在 DeadEndAgent.prepare_dependencies 准备好
)
```

**(d) 注册 `call_finder_agent` tool**（仿 `call_shell_agent`，`executor.py:679` 风格）：
```python
@supervisor.agent.tool
async def call_finder_agent(ctx: RunContext[SupervisorDeps], prompt: str) -> str:
    """Call the finder agent to identify CMS / tech stack of the target."""
    if ctx.deps.finder_agent is None or ctx.deps.finder_deps is None:
        return "Finder agent dependencies not configured."
    memory_prefix = _memory_prompt_prefix(ctx.deps.memory_context)
    result = await ctx.deps.finder_agent.run(
        f"{memory_prefix}{prompt}",
        deps=ctx.deps.finder_deps,
        message_history=ctx.deps.message_history,
        usage=ctx.usage,
        usage_limits=ctx.deps.usage_limits,
        deferred_tool_results=ctx.deps.deferred_tool_results,
    )
    if hasattr(result, "output") and isinstance(result.output, AgentOutput):
        _add_agent_output_to_context(
            task=task_node.task, context=ctx.deps.context,
            agent_name="finder", output=result.output,
        )
        result_str = _format_tool_result_for_supervisor("finder", result.output)
    else:
        result_output = result.output if hasattr(result, "output") else result
        result_str = _format_tool_result_for_supervisor("finder", result_output)
    return result_str
```

### 1.6 准备 Deps（DeadEndAgent.prepare_dependencies）

在 `pobi_agent/pobi_agent.py:396` 的 `prepare_dependencies` 中构建 `self.finder_deps`（类似 `self.shell_deps`），确保类型与 Agent 期望一致。

### 1.7（可选）让 supervisor 优先调用

在威胁建模 prompt（`pobi_agent.py:487` 区域）补充指引，例如："先调用 finder 识别 CMS / 技术栈，再据此决定后续侦查路径"。

---

## 2. 如何新增 / 修改提示词

所有提示词是 `pobi_prompts/` 下的 Jinja2 模板，分为两类：

| 模板 | 渲染函数 | 用途 |
|---|---|---|
| `<agent_name>.instructions.jinja2` | `render_agent_instructions(agent_name, tools)` | 决定 Agent 的人设、策略、工具清单、输出格式 |
| `tools/<tool_name>.description.jinja2` | `render_tool_description(tool_name)` | 决定单个 tool 的用途说明，供模型选择 |

渲染机制（`pobi_prompts/template_renderer.py`）：
- Agent 指令模板中 `tools` 字典会把每个 tool 的 description 注入，典型用法见 `shell.instructions.jinja2:180`：
  ```jinja
  ## 可用工具
  {% for tool_name, tool_description in tools.items() %}
  ### {{tool_name}}
  {{tool_description}}
  {% endfor %}
  ```
- `supervisor.instructions.jinja2` 还会收到 `available_agents` 参数，列出所有可被调用的子 Agent。

### 2.1 修改现有 Agent 的提示词

直接编辑对应 `*.instructions.jinja2`。例如让 `shell_agent` 知道新的 Kali 工具，在 `shell.instructions.jinja2` 的"工具分类"（L196-225）与 `sandboxed_shell_tool.description.jinja2` 的 `AVAILABLE SECURITY TOOLS`（L26-32）各加一行：

```jinja
指纹识别：whatweb、finder
```

⚠️ **模型并不知道 Kali 真实装了什么**——它只依据模板里写死的清单做工具选择。任何新工具都必须显式列进模板，否则模型不会主动使用。

### 2.2 新增一个提示词模板（配合新 Agent / 新工具）

- 新 Agent：新建 `pobi_prompts/<agent_name>.instructions.jinja2`，并用 `render_agent_instructions("agent_name", tools={...})` 渲染。
- 新工具：新建 `pobi_prompts/tools/<tool_name>.description.jinja2`，并用 `render_tool_description("tool_name")` 渲染。
- 共享片段放在 `pobi_prompts/_shared/`，用 `{% include '_shared/xxx.jinja2' %}` 引用。

### 2.3 带参数的渲染

`render_agent_instructions` / `render_tool_description` 接受 `**kwargs`，会作为 Jinja2 变量传给模板。例如 supervisor 渲染时传入 `available_agents=...`（`supervisor_agent.py:30`）。

---

## 3. 如何定义工具调用

"工具"是 Agent 能调用的最小能力单元。两种实现形态：

1. **纯 Python 函数 tool**：直接实现逻辑（如 `webapp_analyzer`）。
2. **走 Kali 的命令 tool**：通过 `shell_runner.run_command(...)` 在共享 Kali 容器内执行外部命令 / 脚本（如 `sandboxed_shell_tool`）。

### 3.1 工具函数签名规范

```python
from pydantic_ai import RunContext
from pobi_agent.utils.structures import WebappreconDeps

def my_tool(ctx: RunContext[WebappreconDeps], arg: str) -> str:
    """工具的单行摘要（模型据此理解何时调用）。

    详细描述写在这里：参数含义、返回内容、何时使用、约束。
    """
    deps = ctx.deps
    # ... 实现 ...
    return "结果文本"
```

- 第一个参数必须是 `RunContext[DepsType]`，通过 `ctx.deps` 取能力。
- **docstring 非常重要**：模型靠它决定何时调用该 tool。
- 返回类型用 `str`（或 Pydantic model），会被拼回对话上下文。

### 3.2 用装饰器上报事件（推荐）

`pobi_agent/tools/tool_wrappers.py` 提供 `with_tool_events`，可在工具执行前后向 event hooks 发 `TOOL_CALL_START` / `TOOL_CALL_END`，前端实时展示：

```python
from pobi_agent.tools.tool_wrappers import with_tool_events

@with_tool_events("finder")
def finder(ctx: RunContext[WebappreconDeps], target: str) -> str:
    ...
```

启用审批模式时（`enable_approval_mode()`），所有被包装的工具会先请求用户批准再执行。

### 3.3 走 Kali 执行外部命令（关键模式）

共享 Kali 容器由 `get_or_create_shared_kali()` 提供，`shell_runner` 指向它。在工具里：

```python
from pobi_agent.sandbox import SandboxStatus
from pobi_agent.tools.tool_wrappers import with_tool_events

@with_tool_events("finder")
def finder(ctx: RunContext[WebappreconDeps], target: str) -> str:
    """在 Kali 中运行指纹识别，识别目标 CMS / 技术栈。"""
    if ctx.deps.shell_runner.sandbox.status != SandboxStatus.RUNNING:
        return "[finder] Kali 沙箱未运行"
    # 方式 A：用 Kali 预装工具
    cmd = f"whatweb {target}"
    # 方式 B：用你自己放在 Kali 里的脚本
    # cmd = f"python3 /opt/finder/finder.py {target}"
    result = ctx.deps.shell_runner.run_command(cmd, timeout_seconds=120)
    return "\n".join(
        log.stdout for log in result.values() if log.stdout
    ) or "[finder] 无输出"
```

> 前提：Kali 镜像（`docker-compose.yml` 的 `kali` service）必须常驻，且目标工具已预装进镜像。

### 3.4 把工具挂到现有 Agent（不新增 Agent）

以给 `requester` / `shell` 增加 `finder` 为例：

**(a) 导出工具**：在 `pobi_agent/tools/__init__.py` 添加
```python
from .finder import finder
```
并加入 `__all__` 列表。

**(b) 挂到 Agent**：编辑对应 `generic_agents/*.py`，如 `request_agent.py`：
```python
from pobi_agent.tools import finder
# tools_metadata 增加：
"finder": render_tool_description("finder"),
# tools=[...] 增加：
Tool(finder, requires_approval=requires_approval),
```

**(c) 写工具描述**：`pobi_prompts/tools/finder.description.jinja2`（参考 `webapp_analyzer.description.jinja2`）。

完成后 supervisor 即可在规划时自主调用该 tool（若该 Agent 是 supervisor 已注册的子 Agent）。

---

## 4. 端到端示例：给威胁建模加一个"Kali 指纹识别"能力

最小改动路线（不新增 Agent，仅新增 tool）：

1. `pobi_agent/tools/finder/finder.py` + `__init__.py`：实现 `finder`，底层 `shell_runner.run_command("whatweb ...")`。
2. `pobi_agent/tools/__init__.py`：导出 `finder`。
3. `pobi_prompts/tools/finder.description.jinja2`：写用途描述。
4. `pobi_agent/agents/generic_agents/shell_agent.py`：把 `Tool(finder)` 挂到 `shell_agent`（因其用 `WebappreconDeps` 含 `shell_runner`）。
5. `pobi_prompts/shell.instructions.jinja2` + `sandboxed_shell_tool.description.jinja2`：在工具清单加 `指纹识别：whatweb、finder`，让模型知道它的存在。
6. （可选）`pobi_agent.py` 威胁建模 prompt 加"优先指纹识别"指引。

完整 Agent 路线（独立 finder Agent）见第 1 节 1.1–1.7。

---

## 5. 常见坑

- **工具未列进提示词 → 模型不用**：模型只依据 `*.instructions.jinja2` / `*.description.jinja2` 里的清单选择工具，Kali 实际装了不代表模型会调。
- **Deps 类型不匹配**：Agent 构造时 `deps_type` 必须与运行时传入的 deps 实例类型一致，否则 `ctx.deps` 取不到字段。
- **忘记注册 `call_*_agent` tool**：新增子 Agent 后，必须在 supervisor 上注册对应 tool，否则 supervisor 无法调用它。
- **Kali 沙箱未启动**：所有 `shell_runner` 调用依赖 `kali` service 常驻；工具里应先检查 `SandboxStatus.RUNNING`。
- **幻觉风险**：工具结果必须来自真实命令输出，提示词模板已强制"不编造"（见 `shell.instructions.jinja2:309` 事实真相段）。
- **AVFS 命名空间**：子 Agent 必须以与父 `DeadEndAgent` 相同的 `session_id` 访问 memory workspace，否则报 "AVFS workspace 'memory' is not mounted"（`executor.py:113` 注释）。
