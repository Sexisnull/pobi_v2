"""Authorization scope gate for the POBI autonomous pentest agent.

授权范围策略：一份小 YAML 文件，由平台层 ``deadend_runner`` 按任务隔离落盘
到 ``tasks/<task_id>/scope.{task_id}.yaml``，由内核 ``pw_requester._scope_path``
按 session_id 命名约定读取。

设计原则
--------
* **Fail safe, opt-in.** 策略 ``enabled=False``（默认）时闸门为 no-op；
  配合 ``load_scope_dict`` 在 ``path=None`` 时返回 ``DEFAULT_SCOPE`` 的行为，
  兼容现有 CTF / demo 工作流。``enabled=True`` 且 in-scope 为空时
  fail-closed（拒绝所有）。
* **显式排除优先.** ``out_of_scope`` 条目即便命中 in-scope 也直接拒绝。
* **单源契约.** 旧的 ``DEFAULT_PATH`` / ``SCOPE_DIR`` / ``DEFAULT_SCOPE``
  全局单例已删除（死代码）：授权范围不再共享全局文件，统一按任务隔离。
  ``path=None`` 给 ``load_scope_dict`` / ``get_scope_policy`` /
  ``check_scope`` 时仍返回 ``DEFAULT_SCOPE`` 默认禁用策略，
  避免运行时路径缺失导致误放行。
"""
from __future__ import annotations

import ipaddress
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from pobi_agent.logging import get_module_logger

logger = get_module_logger(__name__)

DEFAULT_SCOPE: dict[str, Any] = {
    "enabled": False,
    "root_domains": [],   # apex + all subdomains allowed
    "domains": [],        # exact-match only (no subdomains)
    "ips": [],            # single IPs or CIDRs
    "out_of_scope": [],   # explicit excludes (domain / IP / CIDR)
    "max_qps": 10,
    "max_bytes": 5_000_000,
}

_HOST_RE = re.compile(r"^[a-z0-9.\-]+$")


def _normalize_host(value: str) -> str:
    """Pull a bare lowercase host out of a URL / host:port / netloc string."""
    value = (value or "").strip().lower()
    if not value:
        return ""
    if "://" in value:
        value = urlparse(value).netloc or value
    # strip any userinfo and port
    value = value.split("@")[-1]
    if ":" in value:
        value = value.split(":", 1)[0]
    return value


def _as_network(value: str) -> ipaddress._BaseNetwork | None:
    """Return an ``ip_network`` (hosts become /32) or ``None`` if not an IP."""
    try:
        return ipaddress.ip_network(str(value).strip(), strict=False)
    except ValueError:
        return None


def _format_network(net: ipaddress._BaseNetwork) -> str:
    """Render a single-host network without the ``/32`` suffix."""
    if getattr(net, "num_addresses", 0) == 1:
        return str(net.network_address)
    return str(net)


# Module-level cache so repeated per-request loads don't hit disk every time.
_cache: dict[str, Any] = {"mtime": None, "policy": None}


class ScopeViolation(Exception):
    """Raised when a target falls outside the authorized scope."""


class ScopePolicy:
    """In-memory representation of an authorization scope policy."""

    def __init__(self, data: dict[str, Any] | None = None):
        data = data or {}
        self.enabled: bool = bool(data.get("enabled", DEFAULT_SCOPE["enabled"]))
        self.root_domains: list[str] = [
            _normalize_host(d) for d in (data.get("root_domains") or []) if d
        ]
        self.domains: list[str] = [
            _normalize_host(d) for d in (data.get("domains") or []) if d
        ]
        self.ips: list[ipaddress._BaseNetwork] = [
            net for net in (_as_network(i) for i in (data.get("ips") or []) if i) if net
        ]
        # out_of_scope entries are (kind, value) tuples: ("domain", str) | ("ip", network)
        self.out_of_scope: list[tuple[str, Any]] = []
        for o in data.get("out_of_scope") or []:
            if not o:
                continue
            net = _as_network(o)
            if net is not None:
                self.out_of_scope.append(("ip", net))
            else:
                self.out_of_scope.append(("domain", _normalize_host(o)))
        self.max_qps: int = _coerce_int(data.get("max_qps"), DEFAULT_SCOPE["max_qps"])
        self.max_bytes: int = _coerce_int(data.get("max_bytes"), DEFAULT_SCOPE["max_bytes"])

    # -- matching helpers ------------------------------------------------- #
    def _match_domains(self, host: str) -> bool:
        for d in self.domains:
            if host == d:
                return True
        for rd in self.root_domains:
            if host == rd or host.endswith("." + rd):
                return True
        return False

    def _match_ips(self, host: str) -> bool:
        net = _as_network(host)
        if net is None:
            return False
        return any(net.network_address in scope_net for scope_net in self.ips)

    @staticmethod
    def _entry_matches(entry: tuple[str, Any], host: str) -> bool:
        kind, val = entry
        if kind == "ip":
            net = _as_network(host)
            return net is not None and net.network_address in val
        return host == val or host.endswith("." + val)

    # -- public API ------------------------------------------------------- #
    def is_allowed(self, url_or_host: str) -> tuple[bool, str]:
        """Return ``(allowed, reason)`` for a URL or bare host."""
        if not self.enabled:
            return True, "scope gate disabled"
        host = _normalize_host(url_or_host)
        if not host:
            return False, "could not parse host"
        for entry in self.out_of_scope:
            if self._entry_matches(entry, host):
                label = entry[1] if isinstance(entry[1], str) else _format_network(entry[1])
                return False, f"explicitly excluded ({label})"
        if self._match_domains(host) or self._match_ips(host):
            return True, "in scope"
        return False, "no matching in-scope target"

    def check(self, url_or_host: str) -> bool:
        """Raise :class:`ScopeViolation` when the target is not allowed.

        No-op (returns ``True``) when the gate is disabled.
        """
        allowed, reason = self.is_allowed(url_or_host)
        if not allowed:
            raise ScopeViolation(
                f"Scope gate blocked out-of-scope target {url_or_host!r}: {reason}"
            )
        return True


def _coerce_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def load_scope_dict(path: Path | str | None = None) -> dict[str, Any]:
    """读取授权范围 YAML，合并到默认禁用策略。

    - ``path`` 缺省时返回 ``DEFAULT_SCOPE``（gate disabled, no-op）：
      兼容 CLI / 单测场景依赖 ``check_scope(url)`` 路径缺失不报错的旧用法。
    - 文件不存在或解析失败时返回 ``DEFAULT_SCOPE`` 副本。
    """
    import yaml

    if path is None:
        return dict(DEFAULT_SCOPE)
    p = Path(path)
    if not p.exists():
        return dict(DEFAULT_SCOPE)
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        logger.warning("scope: failed to load %s, falling back to default.", p)
        return dict(DEFAULT_SCOPE)
    merged = dict(DEFAULT_SCOPE)
    for key in DEFAULT_SCOPE:
        if key in raw and raw[key] is not None:
            merged[key] = raw[key]
    return merged


def get_scope_policy(path: Path | str | None = None) -> ScopePolicy:
    """按 mtime 缓存加载策略。``path=None`` 返回默认禁用策略。"""
    if path is None:
        return ScopePolicy({"enabled": False})
    p = Path(path)
    try:
        mtime = p.stat().st_mtime
    except FileNotFoundError:
        return ScopePolicy({"enabled": False})
    if _cache.get("policy") is not None and _cache.get("mtime") == mtime:
        return _cache["policy"]  # type: ignore[return-value]
    policy = ScopePolicy(load_scope_dict(p))
    _cache.update(mtime=mtime, policy=policy)
    return policy


def check_scope(url_or_host: str, path: Path | str | None = None) -> bool:
    """网络出口便捷闸门。越权抛 :class:`ScopeViolation`；策略 disabled 时 no-op。"""
    return get_scope_policy(path).check(url_or_host)
