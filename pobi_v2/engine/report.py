"""M5 结构化报告：从任务发现与运行轨迹生成渗透测试报告。

支持：
- ``build_report``：聚合 findings / task_events / artifacts，产出结构化 dict。
- ``render_markdown``：把报告渲染为 Markdown（前端展示 / 导出）。
- ``render_json``：导出机器可读 JSON。
"""
from __future__ import annotations

import json
from collections import Counter
from datetime import datetime
from uuid import UUID

from pobi_v2.db.models import Artifact, ArtifactKind, Finding, Severity, Task, TaskEvent


SEVERITY_ORDER = {
    Severity.critical: 0,
    Severity.high: 1,
    Severity.medium: 2,
    Severity.low: 3,
    Severity.info: 4,
}


def build_report(
    task: Task,
    findings: list[Finding],
    events: list[TaskEvent],
    artifacts: list[Artifact],
) -> dict:
    """聚合结构化报告数据。"""
    sorted_findings = sorted(
        findings, key=lambda f: (SEVERITY_ORDER.get(f.severity, 9), f.created_at)
    )
    severity_counts = Counter(f.severity.value for f in sorted_findings)

    # 运行轨迹按类型计数（思考 / 工具调用 / 状态流转）
    event_types = Counter(e.event_type for e in events)

    # 高危工具调用（来自 tool_call_start 事件）
    tool_calls = [
        e.payload for e in events
        if e.event_type == "tool_call_start" and isinstance(e.payload, dict)
    ]

    return {
        "task_id": str(task.id),
        "target": getattr(task, "target_id", None),
        "name": task.name,
        "objective": task.objective,
        "status": task.status.value,
        "operator": task.operator,
        "started_at": task.started_at.isoformat() if task.started_at else None,
        "finished_at": task.finished_at.isoformat() if task.finished_at else None,
        "summary": (task.result or "")[:2000],
        "severity_counts": dict(severity_counts),
        "total_findings": len(sorted_findings),
        "findings": [
            {
                "id": str(f.id),
                "task_id": str(f.task_id),
                "target_id": str(f.target_id),
                "created_at": f.created_at.isoformat() if f.created_at else None,
                "title": f.title,
                "severity": f.severity.value,
                "description": f.description,
                "evidence": f.evidence,
                "confidence": f.confidence,
                "cwe": f.cwe,
            }
            for f in sorted_findings
        ],
        "event_counts": dict(event_types),
        "tool_calls": tool_calls,
        "artifacts": [
            {
                "id": str(a.id),
                "kind": a.kind.value,
                "name": a.name,
                "content_type": a.content_type,
            }
            for a in artifacts
        ],
    }


def render_markdown(report: dict) -> str:
    """渲染 Markdown 报告。"""
    lines: list[str] = []
    lines.append(f"# 渗透测试报告：{report['name']}")
    lines.append("")
    lines.append(f"- **目标**：{report.get('target')}")
    lines.append(f"- **状态**：{report['status']}")
    lines.append(f"- **操作员**：{report.get('operator')}")
    lines.append(f"- **开始**：{report.get('started_at') or '—'}")
    lines.append(f"- **结束**：{report.get('finished_at') or '—'}")
    lines.append("")
    lines.append("## 概要")
    lines.append("")
    lines.append(report.get("summary") or "（无摘要）")
    lines.append("")
    lines.append("## 风险概览")
    lines.append("")
    sc = report.get("severity_counts", {})
    for sev in ("critical", "high", "medium", "low", "info"):
        lines.append(f"- {sev.upper()}：{sc.get(sev, 0)}")
    lines.append("")
    lines.append("## 发现明细")
    lines.append("")
    if not report.get("findings"):
        lines.append("未发现漏洞。")
    else:
        for f in report["findings"]:
            lines.append(f"### [{f['severity'].upper()}] {f['title']}")
            if f.get("cwe"):
                lines.append(f"- CWE：{f['cwe']}")
            if f.get("description"):
                lines.append(f"- 描述：{f['description']}")
            if f.get("evidence"):
                lines.append("- 证据：")
                lines.append("```json")
                lines.append(json.dumps(f["evidence"], ensure_ascii=False, indent=2))
                lines.append("```")
            lines.append("")
    return "\n".join(lines)


def render_json(report: dict) -> str:
    return json.dumps(report, ensure_ascii=False, indent=2, default=str)
