# Copyright (C) 2025 Yassine Bargach
# Licensed under the GNU Affero General Public License v3
# See LICENSE file for full license information.

"""Reporter agent for summarizing assessment findings and writing reports.

This agent has access to the AVFS write tool.  When invoked it analyzes
the execution context, produces a structured markdown report, and
**writes it to disk itself** via the tool — there is no programmatic
write after the LLM call.
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic_ai import Tool
from pydantic_ai.usage import RunUsage, UsageLimits

from pobi_agent.agents.components.validation_strategies import ValidationVerdict
from pobi_agent.config.settings import ModelSpec
from pobi_agent.context.context_engine import ContextEngine
from pobi_agent.logging import logger
from pobi_agent.tools import write_workspace_file
from pobi_prompts import render_agent_instructions, render_tool_description

from .factory import AgentRunner


# ---------------------------------------------------------------------------
# Deps — the reporter only needs a session_id so write_workspace_file can resolve paths
# ---------------------------------------------------------------------------

@dataclass
class ReporterDeps:
    """Minimal deps for the reporter agent."""
    session_id: str


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------

class ReporterAgent(AgentRunner):
    """Reporter agent — summarizes findings and writes reports to AVFS.

    Unlike a passive summarizer, this agent has the ``write_workspace_file`` tool
    and is instructed to write the report file itself during execution.
    ``output_type`` is ``str`` (plain text confirmation) to avoid the
    Azure AI ``tool_choice`` object format incompatibility that occurs
    with pydantic output schemas.
    """

    def __init__(
        self,
        model: ModelSpec,
        validation_type: str | None = None,
        validation_format: str | None = None,
    ):
        tools_metadata = {
            "write_workspace_file": render_tool_description("write_workspace_file"),
        }

        reporter_instructions = render_agent_instructions(
            "reporter",
            tools=tools_metadata,
            validation_type=validation_type or "security assessment",
            validation_format=validation_format or "Information",
        )

        super().__init__(
            name="reporter",
            model=model,
            instructions=reporter_instructions,
            deps_type=ReporterDeps,
            output_type=str,
            tools=[Tool(write_workspace_file)],
        )
        self.description = (
            "The reporter summarizes assessment findings and writes reports."
        )

    async def run(
        self,
        prompt,
        deps,
        message_history,
        usage,
        usage_limits,
        deferred_tool_results=None,
        *args,
        **kwargs,
    ):
        return await super().run(
            prompt=prompt,
            deps=deps,
            message_history=message_history,
            usage=usage,
            usage_limits=usage_limits,
            deferred_tool_results=deferred_tool_results,
        )

    async def summarize_and_write(
        self,
        root_goal: str,
        verdict: ValidationVerdict,
        context: str,
        session_id: str,
    ) -> str:
        """Run the reporter agent to generate and write a report.

        The agent itself calls ``write_workspace_file`` to persist the report.
        We do NOT write programmatically after the call — the agent
        does it.

        Args:
            root_goal: The top-level objective of the assessment.
            verdict: The ValidationVerdict that triggered the report.
            context: Full accumulated execution context.
            session_id: AVFS session identifier (passed via deps).

        Returns:
            The agent's text output (confirmation message).
        """
        prompt = self._build_prompt(root_goal, verdict, context)
        deps = ReporterDeps(session_id=session_id)

        result = await self.run(
            prompt=prompt,
            deps=deps,
            message_history="",
            usage=RunUsage(),
            usage_limits=UsageLimits(request_limit=None),
        )

        output = str(result.output) if hasattr(result, "output") else str(result)
        logger.debug("ReporterAgent output: %s", output[:200])
        return output

    async def summarize_context(self, context_engine: ContextEngine, session_id: str) -> str:
        """Summarize the workflow context and write it.

        Legacy entry point used outside of the validation gate.
        """
        current_context = context_engine.workflow_context
        prompt = (
            "Analyze and summarize the following workflow context into a "
            "security assessment report. Preserve all critical security "
            "information, vulnerabilities, and technical details.\n\n"
            "Write the report to `reports/context_summary.md` using the "
            "write_workspace_file tool.\n\n"
            f"## Workflow Context\n{current_context}"
        )
        deps = ReporterDeps(session_id=session_id)
        result = await self.run(
            prompt=prompt,
            deps=deps,
            message_history="",
            usage=RunUsage(),
            usage_limits=UsageLimits(request_limit=None),
        )

        output = str(result.output) if hasattr(result, "output") else str(result)
        return output

    # ------------------------------------------------------------------
    # Prompt builder
    # ------------------------------------------------------------------

    @staticmethod
    def _build_prompt(
        root_goal: str,
        verdict: ValidationVerdict,
        context: str,
    ) -> str:
        token_line = (
            f"- Validation token found: `{verdict.token}`\n"
            if verdict.token else ""
        )
        return (
            "你正在撰写一份安全评估报告。请分析下方数据，产出一份完整的 "
            "markdown 报告。\n\n"
            "**你必须使用 write_workspace_file 工具来写入报告。** "
            "不要仅仅把报告作为文本返回。\n\n"
            "文件名要求：\n"
            "- 写入到 `reports/` 目录下\n"
            "- 文件名以应用/目标名加上主要漏洞类型命名\n"
            "- 使用小写 kebab-case（连字符分隔）\n"
            "- 推荐格式：`reports/<应用名称>-<漏洞类型>.md`\n"
            "- 示例：`reports/acme-portal-sqli.md`、`reports/shop-api-idor.md`\n"
            "- 若应用名称不明确，则回退到由目标派生的名称\n\n"
            "重要：\n"
            "- 逐字符保留确切可用的 payload\n"
            "- 包含成功请求的完整 HTTP 请求\n"
            "- 包含证明漏洞存在的响应片段\n"
            "- 记录过滤器绕过技术及其所用的确切编码\n"
            "- 标注验证状态（反射型 vs 已执行，是否需要浏览器测试）\n\n"
            f"## 目标\n{root_goal}\n\n"
            f"## 验证结果\n"
            f"- 结论：{'已达成' if verdict.stop else '未达成'}\n"
            f"- 置信度：{verdict.confidence:.2f}\n"
            f"{token_line}"
            f"- 评述：{verdict.critique}\n\n"
            f"## 评估数据\n{context}\n"
        )
