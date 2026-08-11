"""CoreAgent / DeadEndAgent 适配层（M1 提供工厂，M2 接入任务队列时启用）。

这里只做"引擎接入点"的封装，确保上层（M2 的 Worker）能以统一方式
拿到配置好的 CoreAgent / DeadEndAgent，并注册 M1 实现的事件钩子。
"""
from __future__ import annotations

from pobi_agent.core_agent import CoreAgent
from pobi_agent.hooks import set_event_hooks

from pobi_v2.core.config import settings
from pobi_v2.engine.event_bus import PobiV2EventHooks


def install_event_hooks() -> None:
    """在应用启动时调用一次，把 pobi_agent 的事件重定向到 pobi_v2 事件总线。"""
    set_event_hooks(PobiV2EventHooks())


def build_core_agent(name: str = "pobi-v2-agent", tools: list | None = None) -> CoreAgent:
    """构造一个配置好的 CoreAgent 实例。

    Args:
        name: Agent 名称（用于事件/追踪）。
        tools: 工具列表（M2 从 pobi_agent.tools 注入并加护栏）。
    """
    return CoreAgent(
        model=settings.llm_model,
        instructions="你是一名严谨的 Web 安全渗透测试助手，遵循授权范围，禁止猜测。",
        tools=tools or [],
        api_key=settings.llm_api_key,
        api_base=settings.llm_api_base,
        rate_limit_rpm=settings.llm_rate_limit_rpm,
        name=name,
    )


def build_pobi_agent(name: str = "pobi-v2-agent", tools: list | None = None, approval_callback=None):
    """构造重量级 PobiAgent（M5 审批护栏载体）。

    PobiAgent 支持 ``set_approval_callback`` 与 ``requires_approval``，可在高危
    工具调用时挂起并等待人工决策。若 pobi_agent 未提供 PobiAgent（环境受限），
    退化为 CoreAgent。

    Args:
        approval_callback: 异步回调，兼容 ``PobiAgent.set_approval_callback``。
    """
    try:
        from pobi_agent.pobi_agent import PobiAgent  # type: ignore
    except Exception:
        agent = build_core_agent(name=name, tools=tools)
        return agent

    agent = PobiAgent(
        model=settings.llm_model,
        instructions="你是一名严谨的 Web 安全渗透测试助手，遵循授权范围，禁止猜测。",
        tools=tools or [],
        api_key=settings.llm_api_key,
        api_base=settings.llm_api_base,
        name=name,
    )
    if approval_callback is not None:
        agent.set_approval_callback(approval_callback)
    return agent

