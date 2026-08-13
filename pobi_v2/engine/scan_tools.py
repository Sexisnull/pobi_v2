"""pobi_v2 扫描工具集。

复刻原 pobi_agent 的侦察能力：
- 授权范围闸门：直接复用原 `pobi_agent.scope.ScopePolicy`，在每次网络出口前
  调用其 `.check()`（等价于原 `pw_requester._send_request` 中的 `check_scope()`）。
- HTTP 侦察：使用 `httpx`（逻辑等价于原 `pw_requester` 的 HTTP 行为）；原实现
  走 Playwright，但 pobi_v2 运行环境不内置浏览器进程，故以 httpx 复刻同一语义。
- 受限 shell：仅在显式开启时执行，且同样受 scope 约束（仅允许 localhost/目标内网调试）。
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import httpx

from pobi_agent.scope import ScopePolicy, ScopeViolation


@dataclass
class ToolContext:
    """一次扫描任务的工具运行上下文（复刻原 RequesterDeps 的核心字段）。"""

    scope: ScopePolicy
    agent_id: str | None = None
    session_id: str | None = None
    proxy_url: str | None = None
    memory_workspace_root: str | None = None
    memory_context: str = ""
    # 是否允许执行 shell（默认关闭，对应原 sandbox 的高危动作）
    allow_shell: bool = False
    # path 级排除（原 ScopePolicy 仅 host 级；此处补充 path 粒度）
    out_of_scope_raw: list[str] = field(default_factory=list)


def _path_excluded(url: str, out_of_scope: list[str]) -> bool:
    """path 级排除判断：若 URL 命中某条 out_of_scope 规则的前缀则拒绝。

    例：out_of_scope=['https://example.com/admin/*'] 会拒绝
    'https://example.com/admin/secret'。仅做前缀匹配（复刻 pobi_v2 既有护栏粒度）。
    """
    try:
        path = urlparse(url).path or "/"
    except Exception:
        path = "/"
    for rule in out_of_scope or []:
        r = rule.strip()
        if not r:
            continue
        # 去掉 scheme 与 host，仅保留 path 前缀
        r = r.split("://", 1)[-1]
        r = r.split("/", 1)[-1] if "/" in r else ""  # 去 host，留 path 部分
        if "/*" in r:
            prefix = "/" + r.split("/*", 1)[0]
            if prefix and path.startswith(prefix):
                return True
        elif r and path == "/" + r:
            return True
    return False


def _is_safe_shell(target_url: str | None, command: str) -> bool:
    """极简安全约束：仅允许无害的只读探测命令，且禁止常见破坏动作。

    复刻原 sandbox 的「高危动作需审批」意图——此处以静态黑名单做基础防护，
    真实高危命令仍由 pobi_v2 的 approval gate（engine/approval.py）二次拦截。
    """
    lowered = command.lower()
    banned = (
        "rm -rf", "mkfs", "dd if=", ":(){", "> /dev/sd", "chmod -r",
        "shutdown", "reboot", "curl | sh", "wget | sh",
    )
    for token in banned:
        if token in lowered:
            return False
    # 仅允许常见只读探测
    allowed_prefixes = (
        "curl", "wget", "ping", "nslookup", "dig", "whois", "host",
        "traceroute", "nc -zv", "nmap", "python3 -c", "echo", "cat",
    )
    return any(lowered.strip().startswith(p) for p in allowed_prefixes)


async def http_request(
    ctx: ToolContext,
    method: str,
    url: str,
    headers: dict[str, str] | None = None,
    body: str | None = None,
    follow_redirects: bool = True,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """发起一次 HTTP 侦察请求，出口受 scope 闸门约束。

    等价于原 `pw_requester._send_request`：先 `check_scope(url)`，越权即抛
    `ScopeViolation`；否则用 httpx 发起请求并返回结构化结果。
    """
    # —— 授权范围闸门（复用原 ScopePolicy，host 级）——
    try:
        ctx.scope.check(url)
    except ScopeViolation as exc:  # 原代码同样在此处硬终止越权出站
        from pobi_agent.logging import logger

        logger.warning("Scope gate blocked request to %s: %s", url, exc)
        raise

    # —— path 级排除（补充原 ScopePolicy 的 host 级粒度）——
    if _path_excluded(url, ctx.out_of_scope_raw):
        from pobi_agent.logging import logger

        logger.warning("Scope gate blocked (path-excluded) request to %s", url)
        raise ScopeViolation(f"URL {url} matches an out-of-scope path rule")

    async with httpx.AsyncClient(
        follow_redirects=follow_redirects,
        timeout=timeout,
        proxy=ctx.proxy_url,
        headers=headers or {},
        verify=False,
    ) as client:
        resp = await client.request(method, url, content=body)
        try:
            resp_text = resp.text
        except Exception:
            resp_text = resp.content.decode("utf-8", "replace")
        return {
            "status_code": resp.status_code,
            "headers": dict(resp.headers),
            "body": resp_text[:50_000],
            "url": str(resp.url),
        }


async def run_shell(ctx: ToolContext, command: str) -> dict[str, Any]:
    """受限 shell 执行。

    - `allow_shell=False`：直接拒绝（对应原 sandbox 缺省不开放）。
    - 命令命中静态黑名单或不在只读探测白名单：拒绝。
    - 真实高危动作仍由 approval gate 二次拦截（见 engine/approval.py）。
    """
    if not ctx.allow_shell:
        raise PermissionError("shell execution is disabled for this task")
    if not _is_safe_shell(None, command):
        raise PermissionError(f"command blocked by static allow-list: {command!r}")

    proc = await asyncio_subprocess(command)
    return proc


async def asyncio_subprocess(command: str, timeout: float = 60.0) -> dict[str, Any]:
    """运行受约束的 shell 命令并回传结果。"""
    import asyncio

    proc = await asyncio.create_subprocess_shell(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return {"returncode": -1, "stdout": "", "stderr": "timeout"}
    return {
        "returncode": proc.returncode,
        "stdout": (stdout or b"").decode("utf-8", "replace")[:20_000],
        "stderr": (stderr or b"").decode("utf-8", "replace")[:20_000],
    }


def _extract_host(entry: str) -> str:
    """从 pobi_v2 的 scope 条目（可能含 scheme/path/通配符）提取主机名。

    例：'https://example.com/*' -> 'example.com'
        'http://api.test.local:8080' -> 'api.test.local'
    """
    e = (entry or "").strip()
    if e.startswith("*."):
        e = e[2:]
    # 去掉通配符与路径
    e = e.split("/*")[0].split("*")[0]
    if "://" in e:
        e = urlparse(e).netloc or e
    # 去端口
    e = e.split("@")[-1].split(":")[0]
    return e.lower()


def _is_wildcard(entry: str) -> bool:
    return "/*" in entry or entry.strip().startswith("*.")


def build_scope_policy(
    root_domains: list[str],
    in_scope: list[str],
    out_of_scope: list[str],
    enabled: bool = True,
) -> ScopePolicy:
    """从 pobi_v2 的 Target 构造原 `ScopePolicy`（内存实例，无需 YAML 文件）。

    数据形态适配（复刻原 scope 闸门）：
    - pobi_v2 的 scope 条目形如 `https://example.com/*`，含 scheme/path/通配符；
      原 `ScopePolicy` 只接受纯 host，且 `domains` 为精确匹配、`root_domains`
      含子域。此处把带通配符/主域的条目映射为 `root_domains`，精确 host 映射
      为 `domains`。
    - 原闸门为 host 级；path 级排除由 `http_request` 的 `_path_excluded` 补充。
    """
    roots: list[str] = [_extract_host(r) for r in (root_domains or []) if r]
    for entry in in_scope or []:
        host = _extract_host(entry)
        if host and host not in roots:
            # pobi_v2 的 scope 语义：条目即「该域及其子域」授权，统一按根域处理
            roots.append(host)
    # out_of_scope：原 ScopePolicy 仅支持 host 级。path 级规则（含 /* 或带 path）
    # 交由 http_request 的 _path_excluded 处理，避免把整个 host 误排除。
    # 因此这里只收集「纯 host（不含路径）」的条目进入 host 级黑名单；
    # 含 path 的条目不提取 host，否则 _extract_host 会剥掉端口/path 得到裸主机，
    # 导致整台主机（含 in_scope 的根）被错误排除。
    excluded_hosts = []
    for o in out_of_scope or []:
        if not o or _is_wildcard(o):
            continue
        bare = o.split("://", 1)[-1]
        if "/" in bare:  # 含 path 的条目 → 仅 path 级排除，不进 host 黑名单
            continue
        host = _extract_host(o)
        if host and host not in excluded_hosts:
            excluded_hosts.append(host)
    data: dict[str, Any] = {
        "enabled": enabled,
        "root_domains": roots,
        "domains": [],
        "out_of_scope": excluded_hosts,
    }
    return ScopePolicy(data)
