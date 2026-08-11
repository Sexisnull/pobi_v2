"""护栏单元测试：验证授权范围判定基于数据库 Target 范围。"""
from __future__ import annotations

from pobi_v2.db.models import Target
from pobi_v2.engine.guardrails import (
    assert_in_scope,
    check_scope,
    is_high_risk_tool,
    policy_from_target,
)


def _target(enabled=True, in_scope=None, out_of_scope=None) -> Target:
    t = Target(name="t", url="https://shop.example.com")
    t.enabled = enabled
    t.in_scope = in_scope or ["example.com"]
    t.out_of_scope = out_of_scope or []
    return t


def test_in_scope_allowed():
    t = _target()
    allowed, reason = check_scope(t, "https://shop.example.com/login")
    assert allowed, reason
    assert "in scope" in reason


def test_subdomain_allowed():
    t = _target(in_scope=["example.com"])
    assert check_scope(t, "https://api.example.com")[0]


def test_out_of_scope_denied():
    t = _target(out_of_scope=["secret.example.com"])
    allowed, reason = check_scope(t, "https://secret.example.com")
    assert not allowed
    assert "excluded" in reason


def test_disabled_gate_fails_open():
    t = _target(enabled=False, in_scope=[])
    # 闸门关闭时 fail-open（demo 友好）
    assert check_scope(t, "https://anything.test")[0]


def test_assert_in_scope_raises_on_violation():
    import pytest
    from pobi_agent.scope import ScopeViolation

    t = _target(enabled=True, in_scope=["example.com"], out_of_scope=["evil.com"])
    with pytest.raises(ScopeViolation):
        assert_in_scope(t, "https://evil.com")


def test_high_risk_tool_detection():
    assert is_high_risk_tool("execute_command")
    assert not is_high_risk_tool("read_file")
