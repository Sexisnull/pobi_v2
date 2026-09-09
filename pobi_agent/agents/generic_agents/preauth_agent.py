# Copyright (C) 2025 Yassine Bargach
# Licensed under the GNU Affero General Public License v3
# See LICENSE file for full license information.

"""前置认证 Agent（PreAuthAgent）：任务创建阶段的 LLM 驱动登录。

与 AuthenticatorAgent（任务运行期，依赖 recon 已发现登录面）不同，
PreAuthAgent 在任务创建阶段运行，此时还没有 recon 上下文。它通过
``observe_login_surface`` 先观察真实登录面（HTML 表单 / JSON API /
HTTP Basic / OAuth 重定向），再由 LLM 决策 ``auth_flow`` 与步骤，
调用 ``authenticate`` 完成登录并落盘 ``preauth`` AuthContext。

凭据来源固定为任务钱包（``CredentialsStore.resolve(target, "preauth")``），
用户名密码不进入 LLM 上下文；输出一律 secret-free。
"""
from __future__ import annotations

from typing import Any

from pydantic_ai import DeferredToolRequests, DeferredToolResults, Tool
from pydantic_ai.usage import RunUsage, UsageLimits

from pobi_agent.agents.factory import AgentOutput, AgentRunner
from pobi_agent.config.settings import ModelSpec
from pobi_agent.tools import (
    authenticate,
    observe_login_surface,
    refresh_auth_context,
    validate_auth_context,
)
from pobi_prompts import render_agent_instructions, render_tool_description


class PreAuthOutput(AgentOutput):
    """前置认证输出（继承 AgentOutput 四字段）。

    约束：任何字段不得包含真实 cookie 值、token、密码或用户名。
    """

    pass


class PreAuthAgent(AgentRunner):
    """任务创建阶段的前置认证子代理（LLM 驱动，替换固定 form 分支）。

    工具集：``observe_login_surface``（观察登录面）+ ``authenticate`` +
    ``validate_auth_context`` + ``refresh_auth_context``。

    成功时通过 ``authenticate`` 把 AuthContext 落盘到
    ``tasks/<task_id>/agent/auth_context/preauth.*``（依赖调用方注入
    task_root），供 pre_recon / L0 认证后爬取与 exploitation 复用。
    """

    def __init__(
        self,
        model: ModelSpec,
        deps_type: Any | None,
        target_information: str,
        requires_approval: bool,
        phase: str | None = None,
    ) -> None:
        tools_metadata = {
            "observe_login_surface": render_tool_description("observe_login_surface"),
            "authenticate": render_tool_description("authenticate"),
            "validate_auth_context": render_tool_description("validate_auth_context"),
            "refresh_auth_context": render_tool_description("refresh_auth_context"),
        }

        self.instructions = render_agent_instructions(
            agent_name="preauth",
            tools=tools_metadata,
            target=target_information,
        )

        super().__init__(
            name="preauth",
            model=model,
            instructions=self.instructions,
            deps_type=deps_type,
            output_type=[PreAuthOutput, DeferredToolRequests],
            tools=[
                Tool(observe_login_surface, requires_approval=requires_approval),
                Tool(authenticate, requires_approval=requires_approval),
                Tool(validate_auth_context, requires_approval=requires_approval),
                Tool(refresh_auth_context, requires_approval=requires_approval),
            ],
            phase=phase,
        )

    async def run(
        self,
        prompt,
        deps,
        message_history,
        usage: RunUsage | None,
        usage_limits: UsageLimits | None,
        deferred_tool_results: DeferredToolResults | None = None,
    ):
        return await super().run(
            prompt=prompt,
            deps=deps,
            message_history=message_history,
            usage=usage,
            usage_limits=usage_limits,
            deferred_tool_results=deferred_tool_results,
        )


__all__ = ["PreAuthAgent", "PreAuthOutput"]
