"""M5 审批护栏与结构化报告测试（不依赖 Postgres，覆盖纯逻辑层）。"""
from __future__ import annotations

from uuid import uuid4

from pobi_v2.engine.approval import evaluate_tool_call
from pobi_v2.engine.report import build_report, render_json, render_markdown
from pobi_v2.db.models import (
    ApprovalStatus,
    Artifact,
    ArtifactKind,
    Finding,
    Severity,
    Task,
    TaskStatus,
    TaskEvent,
)
from datetime import datetime, timezone


def test_evaluate_tool_call_case_insensitive():
    assert evaluate_tool_call("Execute_Command") is True
    assert evaluate_tool_call("shell") is True
    assert evaluate_tool_call("curl") is False
    assert evaluate_tool_call("nuclei") is False


def test_evaluate_tool_call_custom_set():
    assert evaluate_tool_call("exfiltrate", high_risk_tools={"exfiltrate"}) is True
    assert evaluate_tool_call("shell", high_risk_tools={"exfiltrate"}) is False


def test_build_report_aggregates_findings_and_severity():
    task = Task(
        id=uuid4(),
        target_id=uuid4(),
        tenant_id=uuid4(),
        name="T1",
        objective="test",
        status=TaskStatus.completed,
        operator="op",
        started_at=datetime.now(timezone.utc),
        finished_at=datetime.now(timezone.utc),
    )
    findings = [
        Finding(id=uuid4(), task_id=task.id, target_id=task.target_id,
                title="SQLi", severity=Severity.high, evidence={"url": "x"}),
        Finding(id=uuid4(), task_id=task.id, target_id=task.target_id,
                title="XSS", severity=Severity.critical, evidence={}),
    ]
    events = [
        TaskEvent(id=uuid4(), task_id=task.id, seq=1, event_type="agent_thought",
                  payload={"thought": "plan"}),
        TaskEvent(id=uuid4(), task_id=task.id, seq=2, event_type="tool_call_start",
                  payload={"tool_name": "curl", "args": "--help"}),
    ]
    artifacts = [
        Artifact(id=uuid4(), task_id=task.id, kind=ArtifactKind.report, name="rep.md"),
    ]
    report = build_report(task, findings, events, artifacts)
    assert report["total_findings"] == 2
    # critical 排在 high 之前
    assert report["findings"][0]["severity"] == "critical"
    assert report["severity_counts"]["critical"] == 1
    assert report["severity_counts"]["high"] == 1
    assert report["event_counts"]["tool_call_start"] == 1
    assert report["tool_calls"][0]["tool_name"] == "curl"
    assert report["artifacts"][0]["kind"] == "report"


def test_render_markdown_contains_findings():
    task = Task(id=uuid4(), target_id=uuid4(), tenant_id=uuid4(), name="T",
                objective="o", status=TaskStatus.completed, operator="op")
    findings = [Finding(id=uuid4(), task_id=task.id, target_id=task.target_id,
                        title="RCE", severity=Severity.critical, evidence={"p": 1})]
    md = render_markdown(build_report(task, findings, [], []))
    assert "# 渗透测试报告" in md
    assert "RCE" in md
    assert "CRITICAL" in md


def test_render_json_roundtrip():
    task = Task(id=uuid4(), target_id=uuid4(), tenant_id=uuid4(), name="T",
                objective="o", status=TaskStatus.completed, operator="op")
    js = render_json(build_report(task, [], [], []))
    import json

    data = json.loads(js)
    assert data["name"] == "T"
    assert data["total_findings"] == 0
