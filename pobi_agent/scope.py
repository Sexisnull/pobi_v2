"""Authorization scope gate for the POBI autonomous pentest agent.

The scope policy is a small YAML document persisted at
``~/.cache/pobi/scope.yaml`` (the exact location the web console writes to).
The web console is the operator's UI for editing it; the agent reads the same
file and enforces it at its single network egress (``pw_requester``).

Design principles
-----------------
* **Fail safe, opt-in.** When the policy is *disabled* (the default) the gate is
  a no-op so existing CTF / demo workflows are unaffected. When *enabled* with
  at least one in-scope entry, every HTTP egress is checked and out-of-scope
  targets are hard-aborted with :class:`ScopeViolation`. An enabled policy with
  no in-scope entries fails closed (denies everything).
* **Explicit exclusions win.** Anything listed in ``out_of_scope`` is denied even
  if it would otherwise match an in-scope rule — letting operators carve out
  internal hosts, third-party CDNs, etc.
* **Single source of truth.** ``DEFAULT_SCOPE`` / ``DEFAULT_PATH`` are imported
  by the web console so both sides stay in sync.
"""
from __future__ import annotations

import ipaddress
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

DEFAULT_PATH = Path.home() / ".cache" / "pobi" / "scope.yaml"

DEFAULT_SCOPE: Dict[str, Any] = {
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


def _as_network(value: str) -> Optional[ipaddress._BaseNetwork]:
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
_cache: Dict[str, Any] = {"mtime": None, "policy": None}


class ScopeViolation(Exception):
    """Raised when a target falls outside the authorized scope."""


class ScopePolicy:
    """In-memory representation of an authorization scope policy."""

    def __init__(self, data: Optional[Dict[str, Any]] = None):
        data = data or {}
        self.enabled: bool = bool(data.get("enabled", DEFAULT_SCOPE["enabled"]))
        self.root_domains: List[str] = [
            _normalize_host(d) for d in (data.get("root_domains") or []) if d
        ]
        self.domains: List[str] = [
            _normalize_host(d) for d in (data.get("domains") or []) if d
        ]
        self.ips: List[ipaddress._BaseNetwork] = [
            net for net in (_as_network(i) for i in (data.get("ips") or []) if i) if net
        ]
        # out_of_scope entries are (kind, value) tuples: ("domain", str) | ("ip", network)
        self.out_of_scope: List[Tuple[str, Any]] = []
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
    def _entry_matches(entry: Tuple[str, Any], host: str) -> bool:
        kind, val = entry
        if kind == "ip":
            net = _as_network(host)
            return net is not None and net.network_address in val
        return host == val or host.endswith("." + val)

    # -- public API ------------------------------------------------------- #
    def is_allowed(self, url_or_host: str) -> Tuple[bool, str]:
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


def load_scope_dict(path: Optional[str] = None) -> Dict[str, Any]:
    """Read the scope YAML, merging onto :data:`DEFAULT_SCOPE`."""
    import yaml

    p = Path(path) if path else DEFAULT_PATH
    if not p.exists():
        return dict(DEFAULT_SCOPE)
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception:
        return dict(DEFAULT_SCOPE)
    merged = dict(DEFAULT_SCOPE)
    for key in DEFAULT_SCOPE:
        if key in raw and raw[key] is not None:
            merged[key] = raw[key]
    return merged


def get_scope_policy(path: Optional[str] = None) -> ScopePolicy:
    """Load (and cache by mtime) the active scope policy."""
    p = Path(path) if path else DEFAULT_PATH
    try:
        mtime = p.stat().st_mtime
    except FileNotFoundError:
        return ScopePolicy({"enabled": False})
    if _cache["policy"] is not None and _cache["mtime"] == mtime:
        return _cache["policy"]
    policy = ScopePolicy(load_scope_dict(p))
    _cache.update(mtime=mtime, policy=policy)
    return policy


def check_scope(url_or_host: str, path: Optional[str] = None) -> bool:
    """Convenience guard for network egress. Raises on violation."""
    return get_scope_policy(path).check(url_or_host)
