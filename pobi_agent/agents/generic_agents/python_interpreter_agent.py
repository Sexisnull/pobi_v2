# Copyright (C) 2025 Yassine Bargach
# Licensed under the GNU Affero General Public License v3
# See LICENSE file for full license information.

"""Python interpreter agent for generating and executing security testing scripts.

This module implements an AI agent that generates Python code for security testing,
vulnerability assessment, and exploit development, then executes the code in the shared Kali Docker sandbox (the same
container used for shell-based attack operations).
"""
from typing import Any
import json
from pydantic_ai import Tool, DeferredToolResults
from pydantic_ai.usage import RunUsage, UsageLimits
from pobi_agent.config.settings import ModelSpec
from pobi_agent.agents.factory import AgentRunner, AgentOutput
from pobi_agent.logging import logger
from pobi_agent.tools import read_auth_storage, run_python_file
from pobi_prompts import render_agent_instructions, render_tool_description

class PythonInterpreterOutput(AgentOutput):
    """Output model for Python interpreter agent execution results.

    Inherits from AgentOutput: detailed_summary, proofs, confidence_score, thoughts
    """
    pass


class PythonInterpreterAgent(AgentRunner):
    """AI agent for generating and executing Python security testing scripts.
    
    This agent specializes in creating Python code for security research tasks
    such as vulnerability testing, exploit development, and security analysis.
    The agent generates Python scripts based on security testing goals and
    executes them in the shared Kali Docker sandbox for safe testing.
    
    The agent uses the `run_python_file` tool which combines writing Python code
    to a file and executing it in an isolated sandbox, ensuring safe execution
    of security testing scripts.
    """

    def __init__(
        self,
        model: ModelSpec,
        deps_type: Any | None,
    ):
        """Initialize the Python interpreter agent.
        
        Args:
            model: The AI model to use for code generation and reasoning.
            deps_type: Optional dependency type for the agent.
            output_type: Optional output type override (defaults to PythonInterpreterOutput).
            tools: Optional list of additional tools (defaults to run_python_file).
        """
        tools_metadata = {
            "read_auth_storage": render_tool_description("read_auth_storage"),
            "run_python_file" : render_tool_description("run_python_file"),

        }
        self.name = "python_interpreter"
        self.instructions = render_agent_instructions(
            agent_name=self.name,
            tools=tools_metadata,
        )

        super().__init__(
            name=self.name,
            model=model,
            instructions=self.instructions,
            deps_type=deps_type,
            output_type=PythonInterpreterOutput,
            tools=[
                Tool(read_auth_storage),
                Tool(run_python_file),
            ]
        )

    async def run(
        self,
        prompt,
        deps,
        message_history,
        usage: RunUsage | None,
        usage_limits: UsageLimits | None,
        deferred_tool_results: DeferredToolResults | None = None,
        *args,
        session_key: str | None = None,
        auth_deps: Any | None = None,
        **kwargs
    ):
        """Execute the agent with a user prompt and optional memory handling.
        
        Runs the agent to generate and execute Python code based on the security
        testing goal. If memory is providedauthz, saves the execution results for
        future reference and context building.
        
        Args:
            user_prompt: The security testing goal or task description.
            deps: Optional dependencies for the agent execution.
            message_history: Previous conversation messages for context.
            usage: Optional usage tracking information.
            usage_limits: Optional usage limits for the execution.
            deferred_tool_results: Optional deferred tool results from previous runs.
            session_key: Legacy session key (host:port)，仅用于旧目录兼容回退。
            auth_deps: 承载 target/agent_id/session_id 的依赖对象（``RequesterDeps``），
                用于经 ``AuthContextHandler`` 定位 tasks/<task_id>/agent/auth_context/
                下的新路径会话。``deps`` 本身是 ``MemoryWorkspaceDeps``，不含这些字段。
        
        Returns:
            AgentRunResult containing the PythonInterpreterOutput with execution results.
        """
        # 沙箱明文通道（2026-09-10，路线 B）：按 RequesterDeps 定位
        # tasks/<task_id>/agent/auth_context/default.json 并取明文，
        # 使模型能把 cookie / Authorization 内联进生成的 Python 代码。
        # 此前传的是字符串 session_key，会走 legacy 分支读旧目录 → 恒返回
        # available=False；且 include_secrets 未显式开启，取不到明文。
        auth_info = json.dumps({"available": False, "note": "no auth deps provided"})
        try:
            if auth_deps is not None:
                auth_info = await read_auth_storage(
                    ctx=auth_deps, profile="default", include_secrets=True
                )
            elif session_key:
                # 退化路径：无 auth_deps 时只能走 legacy 索引（无明文）。
                auth_info = await read_auth_storage(ctx=session_key)
        except Exception as exc:  # noqa: BLE001 - 凭据注入失败不阻断代码生成
            logger.warning("read_auth_storage 注入失败（忽略）: %s", exc)
            auth_info = json.dumps({"available": False, "error": str(exc)})
        prompt_with_auth = f"""\
# Authentication, cookies and other information retrieved from previous tasks
{auth_info}
# Objective and context
{prompt}
"""
        agent_response = await super(PythonInterpreterAgent, self).run(
            prompt_with_auth,
            deps,
            message_history,
            usage,
            usage_limits,
            deferred_tool_results
        )
        return agent_response
