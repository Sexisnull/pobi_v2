"""授权范围护栏：基于 pobi_agent.scope.ScopePolicy。

Web 平台的授权范围来自数据库 Target（in_scope / out_of_scope 列表），
而非原 pobi 的 scope.yaml 文件。本模块复用 `scan_tools.build_scope_policy`
（统一 host 提取 + 通配符→root_domains + path 级分离逻辑），保证入口护栏
与扫描期工具出口闸门使用同一套 ScopePolicy 构造方式。
"""
from __future__ import annotations

from dataclasses import dataclass

from pobi_agent.scope import ScopePolicy, ScopeViolation

from pobi_v2.engine.scan_tools import build_scope_policy


@dataclass
class ScopeRule:
    in_scope: list[str]
    out_of_scope: list[str]
    enabled: bool


def policy_from_target(target) -> ScopePolicy:
    """从数据库 Target 对象构造 ScopePolicy（复用 scan_tools 的统一构造）。"""
    root = _root_from_url(target.url)
    return build_scope_policy(
        root_domains=[root] if root else [],
        in_scope=list(target.in_scope or []),
        out_of_scope=list(target.out_of_scope or []),
        enabled=bool(target.enabled),
    )


def _root_from_url(url: str) -> str | None:
    from urllib.parse import urlparse

    netloc = urlparse(url).netloc or url
    netloc = netloc.split("@")[-1].split(":")[0]
    return netloc.lower() or None


def assert_in_scope(target, url_or_host: str) -> None:
    """在任务入队/执行前校验目标 URL 是否在授权范围内，越权则抛 ScopeViolation。"""
    policy = policy_from_target(target)
    policy.check(url_or_host)


def check_scope(target, url_or_host: str) -> tuple[bool, str]:
    return policy_from_target(target).is_allowed(url_or_host)


# 高危工具白名单（M5 审批护栏使用）：命中需人工确认
HIGH_RISK_TOOLS = {
    "execute_command",
    "run_shell",
    "shell",
    "write_file",
    "exfiltrate",
    "reverse_shell",
}


def is_high_risk_tool(tool_name: str) -> bool:
    return tool_name.lower() in HIGH_RISK_TOOLS
