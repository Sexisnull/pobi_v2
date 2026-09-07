from dataclasses import dataclass
import json
import re
from typing import Any, Literal, AsyncGenerator
import asyncio
from pydantic import BaseModel
from pydantic_ai import DeferredToolResults, RunContext, UsageLimits, RunUsage, UsageLimitExceeded
from pobi_agent.agents import (
    SupervisorAgent, SupervisorOutput, SupervisorDecision,
    RequesterAgent,
    ShellAgent,
    PythonInterpreterAgent, AgentOutput,
    WebAppAnalyzerAgent,
    MemoryAgent,
    AuthenticatorAgent,
)
from pobi_agent.auth_resolver import AuthContextHandler
from pobi_agent.agents.components.planner import TaskNode
from pobi_agent.agents.components.validation_strategies import ValidationGate, ValidationInput
from pobi_agent.agents.reporter import ReporterAgent
from pobi_agent.context import ContextEngine
from pobi_agent.config.settings import ModelSpec
from pobi_agent.tools.avfs.write import write_text
from pobi_agent.utils.structures import MemoryWorkspaceDeps, WebappreconDeps, RequesterDeps, ShellDeps
from pobi_agent.logging import logger
from pobi_agent.agents.components.task_state import window_messages
from pobi_agent.agents.factory import FallbackAgentResult


@dataclass
class SupervisorLoopConfig:
    """Supervisor 驱动循环配置（路径 A）。

    - request_limit：单 ADaPT 迭代内 supervisor 最大轮次。CoreAgent compat 仅把
      request_limit 映射到 limits_dict["requests"] 生效（tool_calls_limit / total_tokens_limit
      在 compat 层被丢弃），故刹车以 request_limit 为准。
    - history_max_messages / history_max_tokens：run() 边界窗口化双约束。
    """
    request_limit: int = 40
    history_max_messages: int = 24
    history_max_tokens: int = 6000

    @property
    def usage_limits(self) -> UsageLimits:
        return UsageLimits(request_limit=self.request_limit, tool_calls_limit=None)


_DEFAULT_SUPERVISOR_LOOP_CONFIG = SupervisorLoopConfig()


class LogEvent(BaseModel):
    """Event representing a log message during execution."""
    type: Literal["log"] = "log"
    message: str


class ResultEvent(BaseModel):
    """Event representing the final execution result."""
    type: Literal["result"] = "result"
    confidence_score: float
    context: dict[str, Any]


class ValidationStopEvent(BaseModel):
    """Event emitted when validation confirms the root objective is solved."""

    type: Literal["validation_stop"] = "validation_stop"
    validation_token: str = ""
    confidence_score: float
    critique: str = ""
    reporter_output: str = ""

# Union type for all possible executor events
ExecutorEvent = LogEvent | ResultEvent | ValidationStopEvent


def _memory_prompt_prefix(memory_context: str) -> str:
    """Render persistent memory context as a prompt prefix for downstream agents."""
    if not memory_context.strip():
        return ""
    return f"## Persistent Memory Context\n{memory_context.strip()}\n\n"


def _build_memory_summary(agent_name: str, task: str, output: AgentOutput) -> str:
    """Create a deterministic memory entry from structured agent output."""
    summary = output.detailed_summary.strip() or "None"
    proofs = output.proofs.strip() or "None"
    thoughts = output.thoughts.strip() or "None"
    return (
        "## Task Summary\n"
        f"- Agent: {agent_name}\n"
        f"- Task: {task.strip()}\n"
        f"- Confidence: {output.confidence_score:.2f}\n"
        f"- Summary: {summary}\n"
        f"- Proofs: {proofs}\n"
        f"- Thoughts: {thoughts}\n\n"
    )


# 侦察文本中需过滤的噪声路径（HTML 标签 / HTTP 版本 / SQL 片段 / 版本号等）。
_RECON_NOISE_PATHS = {
    "/div", "/li", "/ul", "/h1", "/h2", "/h3", "/pre", "/em", "/a",
    "/title", "/1.1", "/2.4.25", "/or", "/union", "/and", "/password",
}
# 常见 Web 技术栈词表（不区分大小写匹配）。
_RECON_TECH_STACK = [
    "PHP", "Apache", "Nginx", "MySQL", "PostgreSQL", "WordPress", "Joomla",
    "Drupal", "Tomcat", "DVWA", "Python", "Django", "Flask", "React",
    "Vue", "jQuery", "Bootstrap", "ASP.NET", "IIS", "Redis", "MongoDB",
    "Node.js", "Express", "Laravel", "Spring", "Ruby", "Rails",
]
# 端点须以这些扩展名或目录特征收尾，过滤掉 HTML 标签 / 版本号等噪声。
_RECON_ENDPOINT_RE = re.compile(
    r"(?P<path>/(?:[a-zA-Z0-9_][a-zA-Z0-9_.\-]*/)*[a-zA-Z0-9_][a-zA-Z0-9_.\-]*"
    r"(?:\.(?:php|html?|asp|aspx|jsp|json|xml|txt|cfg|ini|bak|dist|inc|yml|yaml|env))?)"
)
_AUTH_HINT_RE = re.compile(r"(login|auth|session|signin|logout|oauth|token|captcha)", re.IGNORECASE)


def _parse_recon_endpoints(text: str) -> list[str]:
    """从侦察文本中宽松提取端点路径，过滤 HTML 标签与版本号等噪声。

    仅保留命中扩展名（.php/.html/...）的路径，避免把 /div、/1.1 等噪声写入库。
    IP 地址片段（如 /122.51.72.186）视为噪声一并过滤。
    """
    if not text:
        return []
    found: list[str] = []
    seen: set[str] = set()
    for m in _RECON_ENDPOINT_RE.finditer(text):
        path = m.group("path").rstrip(".")
        low = path.lower()
        if low in _RECON_NOISE_PATHS or len(path) < 2:
            continue
        # 过滤被误判为路径的 IP 地址片段（第一个路径段全为数字与点的组合）。
        first_seg = low.lstrip("/").split("/")[0]
        if re.fullmatch(r"[\d.]+", first_seg):
            continue
        if low not in seen:
            seen.add(low)
            found.append(path)
    return found


def _parse_recon_tech_stack(text: str) -> list[str]:
    """从侦察文本中提取命中的技术栈词（去重，保留原大小写）。"""
    if not text:
        return []
    hits: list[str] = []
    lower_text = text.lower()
    for tech in _RECON_TECH_STACK:
        if tech.lower() in lower_text and tech not in hits:
            hits.append(tech)
    return hits


def _persist_recon_facts(context: "ContextEngine | None", agent_name: str, output: "AgentOutput") -> None:
    """将 agent 输出中的侦察产物（端点 / 技术栈）结构化落本地 recon 库。

    无论侦察还是利用阶段（共用同一 agent），凡在 detailed_summary / thoughts
    中出现的端点与技术栈都旁路写入 recon_facts 与 recon_endpoints，
    使中途取消的任务也能保留可检索的侦察认知。recon_store 未注入时全部 no-op。
    """
    if context is None:
        return
    output_dict = output.model_dump()
    summary = output_dict.get("detailed_summary", "") or ""
    thoughts = output_dict.get("thoughts", "") or ""
    corpus = f"{summary}\n{thoughts}"

    endpoints = _parse_recon_endpoints(corpus)
    for ep in endpoints:
        # 写结构化端点表（含认证面标记）。
        context.add_recon_endpoint(
            path_normalized=ep,
            auth_required=bool(_AUTH_HINT_RE.search(ep)),
            discovered_via=agent_name,
            confidence=0.6,
        )
        # 同时写 recon_facts 维度，便于统一检索。
        context.add_discovered_fact(
            category="endpoint",
            key=ep,
            value=f"Reconnaissance endpoint discovered by {agent_name}: {ep}",
            confidence=0.6,
            source_task=agent_name,
            details={"auth_required": bool(_AUTH_HINT_RE.search(ep))},
            actionable=True,
        )

    for tech in _parse_recon_tech_stack(corpus):
        context.add_recon_technique(name=tech, category="technology", confidence=0.6)
        context.add_discovered_fact(
            category="technology",
            key=tech,
            value=f"Technology stack detected during recon: {tech}",
            confidence=0.6,
            source_task=agent_name,
            actionable=False,
        )


def _format_tool_result_for_supervisor(agent_name: str, output: Any) -> str:
    """Render tool/agent output in a stable format that preserves key details for downstream reasoning.

    切片 3 P2：detailed_summary/proofs/thoughts 限长，避免单轮子 agent 输出全量注入 supervisor 消息列表。
    """
    if isinstance(output, AgentOutput):
        summary = (output.detailed_summary or "None")[:1500]
        proofs = (output.proofs or "None")[:500]
        thoughts = (output.thoughts or "None")[:300]
        return (
            f"{agent_name} agent result\n"
            f"confidence_score: {output.confidence_score:.2f}\n"
            f"detailed_summary:\n{summary}\n\n"
            f"proofs:\n{proofs}\n\n"
            f"thoughts:\n{thoughts}"
        )

    if isinstance(output, BaseModel):
        return (
            f"{agent_name} agent result\n"
            f"{json.dumps(output.model_dump(), indent=2, ensure_ascii=False)[:3000]}"
        )

    return f"{agent_name} agent result\n{str(output)[:3000]}"

@dataclass
class SupervisorDeps:
    """Dependencies for supervisor router containing all agents and their deps."""
    requester_agent: RequesterAgent | None
    requester_deps: RequesterDeps | None
    shell_agent: ShellAgent | None
    shell_deps: ShellDeps | None
    python_interpreter_agent: PythonInterpreterAgent
    webapp_analyzer_agent: WebAppAnalyzerAgent
    memory_agent: MemoryAgent
    memory_deps: MemoryWorkspaceDeps
    session_id: str
    message_history: list | None
    usage_limits: UsageLimits
    deferred_tool_results: DeferredToolResults | None
    authenticator_agent: AuthenticatorAgent | None = None
    memory_context: str = ""
    auth_session_key: str = ""
    context: ContextEngine | None = None  # Context engine for storing agent outputs
    # AVFS 命名空间：memory workspace 由父 DeadEndAgent 以 agent_id 挂载，
    # 子 agent 必须以相同 session_id 访问，否则报 "AVFS workspace 'memory' is not mounted"。
    memory_session_id: str | None = None

class AgentExecutor:
    """Executor component that executes tasks using appropriate agents.
    
    The executor uses a router to determine which specialized agent should handle
    each task, then executes the task with that agent. If no specialized agent is
    available, it falls back to a generic runner.
    
    The executor integrates routing, agent selection, and execution in a single
    component that works within the ADaPT framework.
    """
    def __init__(
        self,
        context: ContextEngine,
        model: ModelSpec,
        available_agents: dict[str, str] | None = None,
        agent_factory: Any | None = None,
        requires_approval: bool = False,
        session_id: str | None = None,
        validation_gate: ValidationGate | None = None,
        reporter: ReporterAgent | None = None,
        memory_session_id: str | None = None,
    ) -> None:
        """Initialize the AgentExecutor.
        
        Args:
            model: Optional AI model for creating specialized agents
            available_agents: Optional dictionary mapping agent names to descriptions
            agent_factory: Optional callback function(agent_name: str, context: dict) -> AgentRunner
            for custom agent creation. If provided, this takes precedence over 
                built-in agent creation.
        """
        self.model = model
        self.available_agents = available_agents or {}
        self.agent_factory = agent_factory
        self.requires_approval = requires_approval
        self.context = context
        self.session_id = session_id
        self.validation_gate = validation_gate
        self.reporter = reporter
        # memory workspace 以 agent_id 命名空间挂载；子 agent 访问须用同一命名空间。
        self.memory_session_id = memory_session_id or session_id
        self.memory_context = ""
        self.auth_session_key = ""

        self.supervisor = SupervisorAgent(
            model=self.model,
            deps_type=None,
            tools=[],
            available_agents=self.available_agents
        )

        self.requester_deps: RequesterDeps | None = None
        self.shell_deps: ShellDeps | None = None
        self.webapprecon_deps: WebappreconDeps | None = None

    def set_dependencies(
        self,
        requester_deps: RequesterDeps | None = None,
        shell_deps: ShellDeps | None = None,
        webapprecon_deps: WebappreconDeps | None = None,
    ) -> None:
        """Register dependency containers for downstream agents."""
        if requester_deps is not None:
            self.requester_deps = requester_deps
        if shell_deps is not None:
            self.shell_deps = shell_deps
        if webapprecon_deps is not None:
            self.webapprecon_deps = webapprecon_deps

    def set_memory_context(self, memory_context: str) -> None:
        """Register startup memory context for downstream agents."""
        self.memory_context = memory_context

    def set_auth_session_key(self, auth_session_key: str) -> None:
        """Register the auth storage session key used by the python interpreter agent."""
        self.auth_session_key = auth_session_key

    def _build_validation_input(
        self,
        confidence_score: float,
        result_context: dict[str, Any],
    ) -> ValidationInput:
        """Convert the latest supervisor result into structured validation input."""
        return ValidationInput(
            task_achieved=result_context.get("task_achieved", False),
            detailed_summary=result_context.get("detailed_summary", ""),
            proofs=result_context.get("proofs", ""),
            confidence_score=confidence_score,
            latest_response=str(result_context.get("supervisor_response", result_context.get("last_output", ""))),
            subagent_log=result_context.get("log", ""),
            supervisor_history=result_context.get("supervisor_history", ""),
        )

    def _build_validation_context(
        self,
        validation_input: ValidationInput,
        max_tokens: int,
    ) -> str:
        """Build a reporter/validator context that preserves the latest iteration details.

        去重（切片 1）：supervisor_history（= agent_context）已包含 unified_context，
        不再单独调 get_unified_context（避免 unified_context ×2）。
        supervisor_history 为空时回退到 get_unified_context。
        """
        sections = []

        supervisor_history = validation_input.supervisor_history.strip()
        if supervisor_history:
            # supervisor_history = unified_context + interaction_history + tasks_context，
            # 已是 supervisor 实际看到的完整输入，直接作为主体。
            sections.append(supervisor_history)
        else:
            sections.append(self.context.get_unified_context(max_tokens=max_tokens))

        latest_supervisor_response = validation_input.latest_response.strip()
        if latest_supervisor_response:
            sections.append(
                "## Latest Supervisor Response\n"
                f"{latest_supervisor_response}"
            )

        latest_subagent_log = validation_input.subagent_log.strip()
        if latest_subagent_log:
            sections.append(
                "## Latest Subagent Execution Log\n"
                f"{latest_subagent_log}"
            )

        return "\n\n".join(section for section in sections if section.strip())

    def _record_supervisor_result_for_validation_stop(
        self,
        task: str,
        validation_input: ValidationInput,
    ) -> None:
        """Persist the latest supervisor synthesis before a validation-triggered exit."""
        if validation_input.detailed_summary:
            self.context.add_agent_response(
                f"[Supervisor] {validation_input.detailed_summary}",
                skip_structured=False,
            )

        if validation_input.proofs:
            self.context.add_discovered_fact(
                source_task=task,
                category="proof",
                key=f"proof_{task[:30]}",
                value=validation_input.proofs,
                confidence=validation_input.confidence_score,
                actionable=not validation_input.task_achieved,
            )

        status_str = "ACHIEVED" if validation_input.task_achieved else "IN PROGRESS"
        self.context.structured.append_to_log(
            f"[Supervisor] Task: {status_str} | Confidence: {validation_input.confidence_score:.2f}"
        )

    async def _run_validation_and_report(
        self,
        validation_input: ValidationInput,
        validation_context: str,
        report_context: str,
    ) -> ValidationStopEvent | None:
        """Validate the root goal and write the success report when solved.

        Validation is executed at the executor boundary so every supervisor
        result is checked consistently, regardless of whether the caller is the
        ADaPT planner or a direct top-level workflow entrypoint.
        """
        if self.validation_gate is None:
            return None
        if not self.context.final_goal:
            return None

        verdict = await self.validation_gate.check(
            output=validation_input,
            root_goal=self.context.final_goal,
            context=validation_context,
        )
        if not verdict.stop:
            return None

        # Build (or reuse) the reporter with the *current task's* validation
        # metadata so flag-format-aware instructions are honored. Falls back to
        # a pre-supplied reporter when one was injected.
        reporter = self.reporter
        if reporter is None:
            validation_type, validation_format = self.validation_gate.validation_metadata()
            reporter = ReporterAgent(
                model=self.model,
                validation_type=validation_type or "security assessment",
                validation_format=validation_format,
            )

        reporter_output = await reporter.summarize_and_write(
            root_goal=self.context.final_goal,
            verdict=verdict,
            context=report_context,
            session_id=str(self.session_id),
        )
        return ValidationStopEvent(
            validation_token=verdict.token,
            confidence_score=verdict.confidence,
            critique=verdict.critique,
            reporter_output=reporter_output,
        )

    async def _refresh_memory_context_for_task(self, task_query: str) -> str:
        """Retrieve task-specific memory immediately before supervisor execution."""
        memory_workspace_root = (
            self.requester_deps.memory_workspace_root
            if self.requester_deps is not None
            else (self.shell_deps.memory_workspace_root if self.shell_deps is not None else None)
        )
        if memory_workspace_root is None or self.session_id is None:
            self.memory_context = ""
            return self.memory_context

        memory_agent = MemoryAgent(
            model=self.model,
            deps_type=MemoryWorkspaceDeps,
        )
        memory_deps = MemoryWorkspaceDeps(
            session_id=self.memory_session_id,
            memory_workspace_root=memory_workspace_root,
        )
        result = await memory_agent.run(
            prompt=(
                f"Current task:\n{task_query}\n\n"
                "Inspect the persistent memory workspace using AVFS tools with workspace=\"memory\". "
                "Return only a concise task-relevant memory summary as plain text for the supervisor. "
                "If memory is empty or not useful for this task, return a short plain-text statement saying that no relevant persisted memory is available."
            ),
            deps=memory_deps,
            message_history=[],
            usage=RunUsage(),
            usage_limits=UsageLimits(request_limit=None, tool_calls_limit=None),
            deferred_tool_results=None,
        )

        output = getattr(result, "output", None)
        self.memory_context = str(output).strip() if output is not None else ""
        if self.shell_deps is not None:
            self.shell_deps.memory_context = self.memory_context
        if self.requester_deps is not None:
            self.requester_deps.memory_context = self.memory_context
        if self.webapprecon_deps is not None:
            self.webapprecon_deps.memory_context = self.memory_context
        return self.memory_context

    async def execute_supervisor(
        self,
        task_node: TaskNode,
        agent_context: str = "",
        usage: RunUsage = RunUsage(),
        usage_limits: UsageLimits | None = None,
        deferred_tool_results: DeferredToolResults | None = None,
        message_history: list | None = None,
        phase: str | None = None,
    ) -> AsyncGenerator[ExecutorEvent, None]:
        """Execute a task using supervisor pattern where router has access to all agents as tools.
        
        This method instantiates all generic agents and makes them available as tools
        to the router agent using agent delegation pattern with RunContext, allowing the 
        router to directly invoke specialized agents as needed.
        
        The execution process:
        1. Instantiates all generic agents (requester, shell, python_interpreter)
        2. Creates a supervisor dependencies dataclass holding all agents and their deps
        3. Creates tool functions using RunContext to delegate to agents
        4. Executes the task with the router, which can now directly call agents
        
        Args:
            task_node: The TaskNode containing the task to execute
            agent_context: Additional context for the agent
            usage: Usage tracking object
            usage_limits: Limits for token usage
            deferred_tool_results: Optional deferred tool results from previous runs
            message_history: Previous conversation messages for context
            
        Yields:
            LogEvent instances for streaming updates.
            The final event is either a ResultEvent for continued execution or
            a ValidationStopEvent when the root task has been solved.
        """

        context: dict[str, Any] = {"log": ""}
        confidence_score: float | None = None

        def emit(message: str) -> LogEvent:
            """Append a log entry to the context and return it for streaming."""
            context["log"] += f"\n{message}"
            return LogEvent(message=message)

        try:
            # await self._refresh_memory_context_for_task(task_node.task)
            yield emit(f"Current task: {task_node.task}\n")
            # Instantiate all generic agents
            requester_agent = RequesterAgent(
                model=self.model,
                deps_type=RequesterDeps,
                target_information=self.context.target,
                requires_approval=self.requires_approval,
                phase=phase,
            ) if self.requester_deps is not None else None

            authenticator_agent = AuthenticatorAgent(
                model=self.model,
                deps_type=RequesterDeps,
                target_information=self.context.target,
                requires_approval=self.requires_approval,
                phase=phase,
            ) if self.requester_deps is not None else None

            shell_agent = ShellAgent(
                model=self.model,
                deps_type=WebappreconDeps,
                target_information=self.context.target,
                requires_approval=self.requires_approval,
                phase=phase,
            ) if self.shell_deps is not None else None

            python_interpreter_agent = PythonInterpreterAgent(
                model=self.model,
                deps_type=MemoryWorkspaceDeps,
            )

            webapp_analyzer_agent = WebAppAnalyzerAgent(
                model=self.model,
                deps_type=RequesterDeps,

            )
            memory_deps = MemoryWorkspaceDeps(
                session_id=self.memory_session_id or "",
                memory_workspace_root=(
                    self.requester_deps.memory_workspace_root
                    if self.requester_deps is not None
                    else (self.shell_deps.memory_workspace_root if self.shell_deps is not None else None)
                ),
                memory_context=self.memory_context,
            )
            memory_agent = MemoryAgent(
                model=self.model,
                deps_type=MemoryWorkspaceDeps,
            )

            # Create supervisor dependencies
            supervisor_deps = SupervisorDeps(
                requester_agent=requester_agent,
                requester_deps=self.requester_deps,
                shell_agent=shell_agent,
                shell_deps=self.shell_deps,
                python_interpreter_agent=python_interpreter_agent,
                webapp_analyzer_agent=webapp_analyzer_agent,
                memory_agent=memory_agent,
                memory_deps=memory_deps,
                session_id=self.session_id or "",
                message_history=message_history,
                usage_limits=usage_limits,
                deferred_tool_results=deferred_tool_results,
                authenticator_agent=authenticator_agent,
                memory_context=self.memory_context,
                auth_session_key=self.auth_session_key,
                context=self.context  # Pass context for storing agent outputs
            )

            # Create new router with agent tools and supervisor deps
            supervisor = SupervisorAgent(
                model=self.model,
                deps_type=SupervisorDeps,
                tools=[],
                available_agents=self.available_agents
            )     
            # Build list of tools for router

            # Helper function to add agent output to context
            def _add_agent_output_to_context(
                task: str,
                context: ContextEngine | None,
                agent_name: str,
                output: AgentOutput
            ) -> None:
                """Add agent output to context for future reference.

                IMPORTANT: No truncation - full content is preserved for supervisor.
                """
                if context is None:
                    return

                output_dict = output.model_dump()

                # Get the simplified fields - NO TRUNCATION
                detailed_summary = output_dict.get("detailed_summary", "")
                proofs = output_dict.get("proofs", "")
                confidence_score = output_dict.get("confidence_score", 0.5)
                thoughts = output_dict.get("thoughts", "")

                # Add detailed summary as discovered fact - FULL content
                if detailed_summary:
                    context.add_discovered_fact(
                        source_task=task,
                        category="agent_result",
                        key=f"{agent_name}_summary",
                        value=detailed_summary,
                        confidence=confidence_score,
                        actionable=True
                    )

                # Add proofs as discovered fact - FULL content
                if proofs:
                    context.add_discovered_fact(
                        source_task=task,
                        category="agent_proofs",
                        key=f"{agent_name}_proofs",
                        value=proofs,
                        confidence=confidence_score,
                        actionable=True
                    )

                # Add thoughts - FULL content (summary auto-generated if empty)
                if thoughts:
                    context.add_thought(
                        agent_name=agent_name,
                        thought=thoughts,
                        summary="",  # Let context auto-generate summary
                    )

                # 侦察产物结构化落库（端点/技术栈），与阶段无关，取消亦可保留。
                _persist_recon_facts(context, agent_name, output)

                # Log the full agent response - NO TRUNCATION
                full_response = f"[{agent_name}]\nSummary: {detailed_summary}\nProofs: {proofs}\nThoughts: {thoughts}"
                context.add_agent_response(
                    full_response,
                    agent_name=agent_name,
                    skip_structured=False
                )

            def _persist_agent_summary(agent_name: str, task: str, output: AgentOutput) -> None:
                """Persist a deterministic summary into the memory workspace.

                MUST use ``self.memory_session_id`` (= agent_id) as the AVFS
                namespace: the memory workspace is mounted under agent_id, not
                task_id. Using session_id (task_id) raises
                ``RuntimeError: AVFS workspace 'memory' is not mounted`` and the
                summary never lands on disk (broke the summary chain 131 times
                historically).
                """
                write_text(
                    f"summaries/{agent_name}.md",
                    _build_memory_summary(agent_name, task, output),
                    session_id=self.memory_session_id,
                    workspace="memory",
                    append=True,
                )

            def _register_auth_facts_in_context(
                task: str,
                context: ContextEngine | None,
                requester_deps: RequesterDeps | None,
            ) -> None:
                """After the AuthenticatorAgent runs, surface saved auth profiles as
                structured ``authentication`` facts in the shared context so other
                agents can reason about which ``auth_profile`` they may use.

                Only secret-free metadata is registered (cookie *names*, storage
                *key names*, header *names*, profile, target slug).
                """
                if context is None or requester_deps is None:
                    return
                target = getattr(requester_deps, "target", None)
                agent_id = getattr(requester_deps, "agent_id", None)
                session_id = getattr(requester_deps, "session_id", None)
                if not target or agent_id is None or session_id is None:
                    return
                try:
                    handler = AuthContextHandler(
                        target=target,
                        agent_id=agent_id,
                        session_id=session_id,
                    )
                    summaries = handler.list_context_summaries()
                except Exception:
                    return
                for profile, summary in summaries.items():
                    if not summary.get("available"):
                        continue
                    target_slug = summary.get("target_slug", "")
                    context.add_discovered_fact(
                        source_task=task,
                        category="authentication",
                        key=f"auth:{target_slug}:{profile}",
                        value="authenticated session available",
                        confidence=1.0,
                        actionable=True,
                        details={
                            "target": summary.get("target"),
                            "target_slug": target_slug,
                            "agent_id": summary.get("agent_id"),
                            "session_id": summary.get("session_id"),
                            "profile": profile,
                            "auth_flow": summary.get("auth_flow"),
                            "auth_type": summary.get("auth_type"),
                            "final_url": summary.get("final_url"),
                            "cookies_count": summary.get("cookies_count"),
                            "cookie_names": summary.get("cookie_names"),
                            "storage_keys": summary.get("storage_keys"),
                            "headers_available": summary.get("headers_available"),
                        },
                    )

            # Create tool functions using RunContext for agent delegation
            async def call_authenticator_agent(ctx: RunContext[SupervisorDeps], prompt: str) -> str:
                """Call the authenticator agent to log in and persist a reusable auth context.

                Use this BEFORE running authenticated tests. After it succeeds,
                downstream agents can pass ``auth_profile="<profile>"`` to
                ``browser_run_steps`` / ``pw_send_payload`` to reuse the session.
                """
                if ctx.deps.authenticator_agent is None or ctx.deps.requester_deps is None:
                    return "Authenticator agent dependencies not configured."
                memory_prefix = _memory_prompt_prefix(ctx.deps.memory_context)
                result = await ctx.deps.authenticator_agent.run(
                    f"{memory_prefix}{prompt}",
                    deps=ctx.deps.requester_deps,
                    message_history=ctx.deps.message_history,
                    usage=ctx.usage,
                    usage_limits=ctx.deps.usage_limits,
                    deferred_tool_results=ctx.deps.deferred_tool_results,
                )
                if hasattr(result, "output") and isinstance(result.output, AgentOutput):
                    _add_agent_output_to_context(
                        task=task_node.task,
                        context=ctx.deps.context,
                        agent_name="authenticator",
                        output=result.output,
                    )
                    try:
                        _persist_agent_summary("authenticator", prompt, result.output)
                    except Exception as _sum_exc:
                        emit(f"[memory] persist authenticator summary failed: {_sum_exc}")
                    _register_auth_facts_in_context(
                        task=task_node.task,
                        context=ctx.deps.context,
                        requester_deps=ctx.deps.requester_deps,
                    )
                    result_str = _format_tool_result_for_supervisor("authenticator", result.output)
                else:
                    result_output = result.output if hasattr(result, "output") else result
                    result_str = _format_tool_result_for_supervisor("authenticator", result_output)
                emit(f"[authenticator] prompt={prompt[:200]} | result={result_str[:300]}")
                return result_str

            async def call_requester_agent(ctx: RunContext[SupervisorDeps], prompt: str) -> str:
                """Call the requester agent to perform HTTP request testing."""
                if ctx.deps.requester_agent is None or ctx.deps.requester_deps is None:
                    return "Requester agent dependencies not configured."
                memory_prefix = _memory_prompt_prefix(ctx.deps.memory_context)
                # 证据驱动收敛：每次委派 requester 前注入失败足迹，提醒转向而非硬试
                footprint_prefix = ""
                try:
                    if ctx.deps.context is not None:
                        fp = ctx.deps.context.get_failed_footprint_summary()
                        if fp:
                            footprint_prefix = f"\n{fp}\n"
                except Exception as _fp_exc:  # noqa: BLE001
                    logger.debug("requester 足迹摘要注入失败（忽略）: %s", _fp_exc)
                # 认证会话复用：authenticator 已落库 authentication facts 时，
                # 告知 requester 已有可用 auth_profile，避免重复登录（ISSUE-002）。
                auth_prefix = ""
                try:
                    if ctx.deps.context is not None:
                        auth_facts = [
                            f for f in getattr(ctx.deps.context, "facts", {}).values()
                            if getattr(f, "category", None) == "authentication"
                        ]
                        if auth_facts:
                            profiles = sorted({
                                str((f.details or {}).get("profile", "?"))
                                for f in auth_facts
                            })
                            auth_prefix = (
                                "\n[已有认证会话可用，auth_profile="
                                f"{profiles}。访问受保护页面时必须在工具参数中传 "
                                "auth_profile 复用会话，禁止重新登录。]\n"
                            )
                except Exception as _auth_exc:  # noqa: BLE001
                    logger.debug("requester 认证会话注入失败（忽略）: %s", _auth_exc)
                result = await ctx.deps.requester_agent.run(
                    f"{memory_prefix}{footprint_prefix}{auth_prefix}{prompt}",
                    deps=ctx.deps.requester_deps,
                    message_history=ctx.deps.message_history,
                    usage=ctx.usage,
                    usage_limits=ctx.deps.usage_limits,
                    deferred_tool_results=ctx.deps.deferred_tool_results
                )
                if hasattr(result, 'output') and isinstance(result.output, AgentOutput):
                    _add_agent_output_to_context(
                        task=task_node.task,
                        context=ctx.deps.context,
                        agent_name="requester",
                        output=result.output
                    )
                    try:
                        _persist_agent_summary("requester", prompt, result.output)
                    except Exception as _sum_exc:
                        emit(f"[memory] persist requester summary failed: {_sum_exc}")
                    result_str = _format_tool_result_for_supervisor("requester", result.output)
                else:
                    result_output = result.output if hasattr(result, "output") else result
                    result_str = _format_tool_result_for_supervisor("requester", result_output)
                emit(f"[requester] prompt={prompt[:200]} | result={result_str[:300]}")
                return result_str

            async def call_shell_agent(ctx: RunContext[SupervisorDeps], prompt: str) -> str:
                """Call the shell agent to execute shell commands."""
                if ctx.deps.shell_agent is None or ctx.deps.shell_deps is None:
                    return "Shell agent dependencies not configured."
                memory_prefix = _memory_prompt_prefix(ctx.deps.memory_context)
                result = await ctx.deps.shell_agent.run(
                    f"{memory_prefix}{prompt}",
                    deps=ctx.deps.shell_deps,
                    message_history=ctx.deps.message_history,
                    usage=ctx.usage,
                    usage_limits=ctx.deps.usage_limits,
                    deferred_tool_results=ctx.deps.deferred_tool_results
                )
                if hasattr(result, 'output') and isinstance(result.output, AgentOutput):
                    _add_agent_output_to_context(
                        task=task_node.task,
                        context=ctx.deps.context,
                        agent_name="shell",
                        output=result.output
                    )
                    try:
                        _persist_agent_summary("shell", prompt, result.output)
                    except Exception as _sum_exc:
                        emit(f"[memory] persist shell summary failed: {_sum_exc}")
                    result_str = _format_tool_result_for_supervisor("shell", result.output)
                else:
                    result_output = result.output if hasattr(result, "output") else result
                    result_str = _format_tool_result_for_supervisor("shell", result_output)
                emit(f"[shell] prompt={prompt[:200]} | result={result_str[:300]}")
                return result_str
            
            async def call_webapp_analyzer_agent(ctx: RunContext[SupervisorDeps], prompt: str) -> str:
                """Call the webapp analyzer agent to analyze web application structure and behavior."""
                memory_prefix = _memory_prompt_prefix(ctx.deps.memory_context)
                result = await ctx.deps.webapp_analyzer_agent.run(
                    f"{memory_prefix}{prompt}",
                    deps=ctx.deps.requester_deps,
                    message_history=ctx.deps.message_history,
                    usage=ctx.usage,
                    usage_limits=ctx.deps.usage_limits,
                    deferred_tool_results=ctx.deps.deferred_tool_results
                )
                result_output = result.output if hasattr(result, "output") else result
                result_str = _format_tool_result_for_supervisor("webapp_analyzer", result_output)
                emit(f"[webapp_analyzer] prompt={prompt[:200]} | result={result_str[:300]}")
                return result_str

            async def call_python_interpreter_agent(ctx: RunContext[SupervisorDeps], prompt: str) -> str:
                """Call the python interpreter agent to execute Python scripts."""
                memory_prefix = _memory_prompt_prefix(ctx.deps.memory_context)
                result = await ctx.deps.python_interpreter_agent.run(
                    f"{memory_prefix}{prompt}",
                    deps=ctx.deps.memory_deps,
                    session_key=ctx.deps.auth_session_key,
                    message_history=ctx.deps.message_history,
                    usage=ctx.usage,
                    usage_limits=ctx.deps.usage_limits,
                    deferred_tool_results=ctx.deps.deferred_tool_results
                )
                if hasattr(result, 'output') and isinstance(result.output, AgentOutput):
                    _add_agent_output_to_context(
                        task=task_node.task,
                        context=ctx.deps.context,
                        agent_name="python_interpreter",
                        output=result.output
                    )
                    try:
                        _persist_agent_summary("python_interpreter", prompt, result.output)
                    except Exception as _sum_exc:
                        emit(f"[memory] persist python_interpreter summary failed: {_sum_exc}")
                    result_str = _format_tool_result_for_supervisor("python_interpreter", result.output)
                else:
                    result_output = result.output if hasattr(result, "output") else result
                    result_str = _format_tool_result_for_supervisor("python_interpreter", result_output)
                emit(f"[python_interpreter] prompt={prompt[:200]} | result={result_str[:300]}")
                return result_str

            async def call_memory_agent(ctx: RunContext[SupervisorDeps], prompt: str) -> str:
                """Call the memory agent to inspect or update persistent notes."""
                result = await ctx.deps.memory_agent.run(
                    prompt,
                    deps=ctx.deps.memory_deps,
                    message_history=ctx.deps.message_history,
                    usage=ctx.usage,
                    usage_limits=ctx.deps.usage_limits,
                    deferred_tool_results=ctx.deps.deferred_tool_results,
                )
                if not hasattr(result, "output"):
                    return str(result)
                output = result.output
                if isinstance(output, BaseModel):
                    return str(output.model_dump())
                return str(output)

            @supervisor.agent.tool
            async def call_recon_lookup(
                ctx: RunContext[SupervisorDeps],
                host: str | None = None,
                tech: str | None = None,
                path_prefix: str | None = None,
                category: str | None = None,
                keyword: str | None = None,
                limit: int = 20,
            ) -> str:
                """检索本任务已物化的侦察资产（端点/技术栈/事实），按需下钻详细信息。

                当索引层信息不足、需要某个端点的详细参数、某技术栈的历史测试结果、
                或按关键词查找已有发现时调用。命中可复用历史结论，避免重复探索。

                Args:
                    host: 按主机精确匹配（如 target.com）
                    tech: 按技术栈/手法匹配（如 nginx、sql-injection）
                    path_prefix: 按端点路径前缀匹配（如 /api/user）
                    category: 按事实类别匹配（如 endpoint、technology、vulnerability）
                    keyword: 关键词全文检索（FTS5 模糊召回）
                    limit: 返回上限（默认 20）
                """
                empty = json.dumps(
                    {"found": False, "count": 0, "results": [], "hint": "无匹配侦察资产"},
                    ensure_ascii=False,
                )
                context = ctx.deps.context
                if context is None or context.recon_store is None:
                    return empty
                try:
                    results = context.recon_store.lookup(
                        task_id=str(ctx.deps.session_id),
                        host=host,
                        tech=tech,
                        path_prefix=path_prefix,
                        category=category,
                        keyword=keyword,
                        limit=limit,
                    )
                except Exception as exc:  # noqa: BLE001 - 工具失败不阻断主流程
                    logger.warning("supervisor recon_lookup 查询失败（已忽略）: %s", exc)
                    return empty
                if not results:
                    return empty
                # 钳制返回体：单条结果截断（防完整 facts 撑爆 supervisor 上下文，
                # 曾致 supervisor 陷入 recon_lookup 查询循环 50 轮不收敛）。
                _MAX_LOOKUP_ITEMS = 20
                _MAX_ITEM_CHARS = 800
                trimmed = []
                for r in results[: _MAX_LOOKUP_ITEMS]:
                    if not isinstance(r, dict):
                        trimmed.append(r)
                        continue
                    r2 = dict(r)
                    for key in ("value", "detail", "description", "content", "path"):
                        v = r2.get(key)
                        if isinstance(v, str) and len(v) > _MAX_ITEM_CHARS:
                            r2[key] = v[:_MAX_ITEM_CHARS] + (
                                f"...[截断：原文 {len(v)} 字符]"
                            )
                    trimmed.append(r2)
                return json.dumps(
                    {"found": True, "count": len(trimmed), "results": trimmed,
                     "hint": "命中本地侦察资产，可复用历史结论（结果已截断，按需缩小 limit 精查）"},
                    ensure_ascii=False, indent=2,
                )

            # [Path A] 子 agent 调用已移出 pydantic-ai 工具边界，下面定义驱动层直调入口
            class _ToolCtx:
                """最小工具上下文垫片，复用既有 call_* 嵌套函数（原 @supervisor.agent.tool）。"""
                def __init__(self, deps, usage):
                    self.deps = deps
                    self.usage = usage

            async def _run_sub_agent(agent_name: str, prompt: str) -> str:
                """驱动层直调子 agent（替代 supervisor 工具调用），复用既有 call_* 逻辑。

                每次调用 message_history=None（独立，不污染 supervisor 历史），
                usage_limits 取自 effective_limits（有界刹车）。落库闭环不变。
                """
                logger.info(
                    "[EXEC] 委派子 agent | task_id=%s | agent=%s | prompt=%s",
                    getattr(task_node, "task_id", "?"), agent_name, (prompt or "")[:120],
                )
                ctx = _ToolCtx(supervisor_deps, usage)
                name = (agent_name or "").lower()
                if name == "authenticator":
                    return await call_authenticator_agent(ctx, prompt)
                if name == "requester":
                    return await call_requester_agent(ctx, prompt)
                if name == "shell":
                    return await call_shell_agent(ctx, prompt)
                if name == "webapp_analyzer":
                    return await call_webapp_analyzer_agent(ctx, prompt)
                if name == "python_interpreter":
                    return await call_python_interpreter_agent(ctx, prompt)
                if name == "memory":
                    return await call_memory_agent(ctx, prompt)
                emit(f"[SUPERVISOR-LOOP] unknown agent: {agent_name}")
                return f"[unknown agent: {agent_name}]"

            # Execute task with supervisor
            supervisor_prompt = f"Your task is : {task_node.task}\n"

            # 前置侦查结构化结论（指纹/WAF/端点综述）：读时现算注入，supervisor 启动即掌握目标形态。
            # 置于 prompt 最前（紧跟任务单），作为 L0 决策依据。
            try:
                if self.context.recon_store is not None:
                    pre_recon = self.context.recon_store.build_pre_recon_json(
                        task_id=self.context._recon_task_id(),
                    )
                    pre_recon_block = json.dumps(
                        pre_recon, ensure_ascii=False, indent=2
                    )
                    if pre_recon_block:
                        supervisor_prompt += (
                            "## 前置侦查结构化结论（指纹/WAF/端点综述/历史威胁）:\n"
                            "<pre_recon_json>\n"
                            f"{pre_recon_block}\n"
                            "</pre_recon_json>\n"
                            "## 复用指引：\n"
                            "若 pre_recon_json.historical_threats 非空，这些是历史任务已建模的漏洞结论，"
                            "请优先对每一项做「存在性验证」：重新探测对应 affected_endpoint/PoC，"
                            "确认漏洞仍存在则复用该结论并标记 confirmed；已修复则明确标注排除；"
                            "在此基础上再补充未被覆盖的新漏洞。禁止对已有结论的端点做重复全量侦查。\n"
                        )
            except Exception as _pr_exc:  # noqa: BLE001
                logger.debug("前置侦查 JSON 注入失败（忽略）: %s", _pr_exc)

            if supervisor_deps.memory_context:
                supervisor_prompt += f"## Persistent Memory Context:\n{supervisor_deps.memory_context}\n"
            if agent_context:
                supervisor_prompt += f"## Traces: \n{agent_context}\n"
            # 证据驱动收敛：注入失败足迹摘要，主控据此转向而非死磕同一攻击面
            try:
                footprint_summary = self.context.get_failed_footprint_summary()
                if footprint_summary:
                    supervisor_prompt += f"\n{footprint_summary}\n"
            except Exception as _fp_exc:  # noqa: BLE001
                logger.debug("足迹摘要注入失败（忽略）: %s", _fp_exc)

            # 发送前日志：输出将发往远端 LLM 的完整 user 提示词（含 pre_recon JSON）。
            # 便于排查"实际发给 LLM 的内容"。
            logger.info(
                "========== SUPERVISOR LLM PROMPT (pre-request) ==========\n%s\n=======================================================",
                supervisor_prompt,
            )

            # ---- [Path A] Supervisor 决策器 + 驱动循环（替代 router 工具）----
            # 裁剪边界在「我们拥有的循环」(run() 之间)：取 raw_messages 剥离 system 后窗口化重传，
            # 根治 message_history 跨轮无界膨胀（L3 主膨胀源）。子 agent 经 _run_sub_agent 直调，
            # 每次独立 message_history（不回灌 supervisor 历史），仅 compact 结果进 history。
            cfg = _DEFAULT_SUPERVISOR_LOOP_CONFIG
            effective_limits = usage_limits or cfg.usage_limits
            # 子 agent 调用：独立历史 + 有界刹车（经 call_* 的 ctx.deps 读取）
            supervisor_deps.message_history = None
            supervisor_deps.usage_limits = effective_limits

            history = message_history if message_history is not None else []
            logger.info(
                "[EXEC] execute_supervisor 启动 | task_id=%s | is_root=%s | initial_history=%d",
                getattr(task_node, "task_id", "?"),
                "yes" if getattr(task_node, "is_root", False) else "no",
                len(history),
            )
            first_round = True
            decision: SupervisorDecision | None = None
            max_rounds = max(1, int(cfg.request_limit or 1))
            outcome_for_next: str = ""
            for _round in range(max_rounds):
                windowed = window_messages(history, cfg.history_max_messages, cfg.history_max_tokens)
                prompt = supervisor_prompt if first_round else outcome_for_next
                first_round = False
                emit(f"[SUPERVISOR-LOOP] round={_round + 1} history={len(history)} windowed={len(windowed)}")
                logger.info(
                    "[EXEC] supervisor 第 %d 轮 | task_id=%s | history=%d | windowed=%d",
                    _round + 1, getattr(task_node, "task_id", "?"), len(history), len(windowed),
                )
                result = await supervisor.run(
                    prompt=prompt,
                    deps=supervisor_deps,
                    message_history=windowed,
                    usage=usage,
                    usage_limits=effective_limits,
                    deferred_tool_results=deferred_tool_results,
                )
                if isinstance(result, FallbackAgentResult):
                    emit("[SUPERVISOR-LOOP] usage limit / error reached; terminating loop.")
                    logger.info("[EXEC] supervisor 触顶/错误终止 | task_id=%s", getattr(task_node, "task_id", "?"))
                    confidence_score = 0.5
                    context["task_achieved"] = False
                    context["detailed_summary"] = (
                        "[SUPERVISOR-LOOP] supervisor loop terminated by usage limit or error before completion."
                    )
                    context["proofs"] = ""
                    context["last_output"] = result.output
                    context["supervisor_response"] = context["detailed_summary"]
                    break
                decision = result.output
                if decision.action == "complete":
                    confidence_score = decision.confidence_score
                    logger.info(
                        "[EXEC] supervisor complete | task_id=%s | task_achieved=%s | confidence=%.2f",
                        getattr(task_node, "task_id", "?"), decision.task_achieved, decision.confidence_score,
                    )
                    context["task_achieved"] = decision.task_achieved
                    context["detailed_summary"] = decision.detailed_summary
                    context["proofs"] = decision.proofs
                    context["last_output"] = decision
                    context["supervisor_response"] = (
                        "Task achieved: "
                        f"{decision.task_achieved}\n"
                        f"Confidence: {decision.confidence_score:.2f}\n"
                        f"Detailed summary:\n{decision.detailed_summary}\n\n"
                        f"Proofs:\n{decision.proofs or 'None'}"
                    )
                    break
                # action == call_agent → 驱动层直调子 agent（不回灌 supervisor 历史）
                outcome = await _run_sub_agent(decision.agent or "", decision.prompt or "")
                # 取回完整对话轮次（含首条任务 user），下一轮以 outcome 为 prompt（不重复追加）
                history.clear()
                history.extend(result.raw_messages[1:] if getattr(result, "raw_messages", None) else [])
                # 同时窗口化存储列表本身，避免跨轮无界累积（L3 根治）
                history[:] = window_messages(history, cfg.history_max_messages, cfg.history_max_tokens)
                outcome_for_next = (
                    f"[Delegation result] agent={decision.agent}\n"
                    f"prompt={decision.prompt or ''}\n"
                    f"{outcome}"
                )
            else:
                # 未达 complete 且未触顶（理论上 usage_limits 已先触顶）→ 兜底
                emit(f"[SUPERVISOR-LOOP] exceeded {max_rounds} rounds without completion.")
                if confidence_score is None:
                    confidence_score = 0.5
                context.setdefault("task_achieved", False)
                context.setdefault("detailed_summary", "[SUPERVISOR-LOOP] exceeded max rounds without completion.")
                context.setdefault("proofs", "")
                context.setdefault("supervisor_response", context.get("detailed_summary", ""))
            context["supervisor_history"] = agent_context

            validation_input = self._build_validation_input(
                confidence_score=confidence_score,
                result_context=context,
            )
            validation_context = self._build_validation_context(
                validation_input,
                max_tokens=12_000,
            )
            report_context = self._build_validation_context(
                validation_input,
                max_tokens=100_000,
            )
            validation_event = await self._run_validation_and_report(
                validation_input,
                validation_context,
                report_context,
            )
            if validation_event is not None:
                self._record_supervisor_result_for_validation_stop(
                    task=task_node.task,
                    validation_input=validation_input,
                )
                yield validation_event
                return

            yield ResultEvent(
                confidence_score=confidence_score,
                context=context,
            )
            return

        except UsageLimitExceeded as exc:
            yield emit(f"[SUPERVISOR] Usage limit reached: {exc}")
            yield ResultEvent(
                confidence_score=confidence_score or 0.5,
                context=context,
            )
            return
        except (GeneratorExit, asyncio.CancelledError):
            # These exceptions must not be caught - re-raise to allow proper cleanup
            raise
        except Exception as exc:
            # Store error info in context before yielding
            # This prevents "generator didn't stop after throw()" if the generator
            # is being closed due to the exception
            context["last_output"] = f"detailed_summary=\"Agent error: {exc}. Agent: supervisor\""
            context["detailed_summary"] = f"Agent error: {exc}. Agent: supervisor"
            try:
                yield emit(f"[SUPERVISOR] Error: {exc}")
                yield ResultEvent(
                    confidence_score=confidence_score or 0.5,
                    context=context,
                )
            except GeneratorExit:
                # If generator is being closed, don't try to yield more
                pass
            return
