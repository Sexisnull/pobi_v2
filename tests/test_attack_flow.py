"""攻击流（Attack Flow）聚合层测试（不依赖 Postgres）。

图谱与时间轴的组装逻辑都是纯函数（入参为字典），可直接单测；租户隔离
走端点函数直调（伪造 session 返回 None 与其他租户对象），不引入 PG 与
应用启动链路。
"""
from __future__ import annotations

import asyncio
import uuid

import pytest

from pobi_v2.core.exceptions import NotFoundError
from pobi_v2.routers import persistence, targets
from pobi_v2.schemas.persistence import TaskEventRangeOut
from pobi_v2.schemas.target import AttackFlowOut


# ---------------------------------------------------------------------- 样本

def _threats():
    return [
        {
            "id": "t1",
            "cve_id": "CVE-2024-0001",
            "title": "RCE in nginx",
            "category": "rce",
            "severity": "critical",
            "status": "exploited",
            "cvss_score": 9.8,
            "target_endpoint": "/api/login",
            "evidence_summary": "PoC 命中",
            "confidence": 0.9,
            "source_tasks": ["task-1"],
        }
    ]


def _facts():
    return [
        {
            "id": "f1",
            "category": "technology",
            "key": "nginx",
            "value": "1.24",
            "confidence": 0.8,
            "source_tasks": ["task-1"],
        }
    ]


def _endpoints():
    return [
        {
            "id": "e1",
            "host": "api.example.com",
            "path": "/api/login",
            "method": "POST",
            "status_code": 200,
            "auth_required": True,
            "tech_stack": ["nginx"],
            "confidence": 0.9,
            "source_tasks": ["task-1"],
        }
    ]


def _findings():
    return [
        {
            "id": "fd1",
            "task_id": "task-1",
            "title": "RCE via CVE-2024-0001",
            "severity": "high",
            "cwe": "CWE-94",
            "description": "",
            "confidence": 0.85,
        }
    ]


# ---------------------------------------------------------------------- 事件分类与时间桶

def test_event_class_mapping():
    assert targets._event_class("tool_call_end") == "tool"
    assert targets._event_class("agent_error") == "error"
    assert targets._event_class("task_status_changed_failed") == "error"
    assert targets._event_class("recon_threat_found") == "recon"
    assert targets._event_class("phase_changed") == "recon"
    assert targets._event_class("validation_result") == "finding"
    assert targets._event_class("llm_iteration") == "other"
    assert targets._event_class("") == "other"


def test_choose_bucket_ms_respects_max_buckets():
    # 1 小时 / 240 桶 → 15s → 落到 30s 阶梯
    assert targets._choose_bucket_ms(0, 3_600_000, 240) == 30_000
    # 极短跨度取最小阶梯
    assert targets._choose_bucket_ms(0, 1_000, 240) == 1_000
    # 退化：起止相同
    assert targets._choose_bucket_ms(5_000, 5_000, 240) == 1_000


def test_bucket_events_aggregates_and_skips_empty():
    points = [(0.0, "recon"), (500.0, "recon"), (1_500.0, "tool")]
    buckets = targets._bucket_events(points, 0.0, 1_000)
    assert [b["index"] for b in buckets] == [0, 1]
    assert buckets[0]["counts"] == {"recon": 2}
    assert buckets[1]["counts"] == {"tool": 1}
    # 桶边界以 start 为基准，第三桶不应出现
    assert len(targets._bucket_events([], 0.0, 1_000)) == 0


# ---------------------------------------------------------------------- 路径匹配

def test_match_paths_full_and_substring():
    assert targets._match_paths("/api/login", "/api/login") is True
    assert targets._match_paths("/api/login", "/api") is True
    assert targets._match_paths("/api", "/api/login") is True
    # 根路径过宽泛，只参与全等
    assert targets._match_paths("/", "/admin") is False
    assert targets._match_paths("/", "/") is True
    assert targets._match_paths("", "/admin") is False


# ---------------------------------------------------------------------- 图谱组装

def test_build_graph_nodes_and_edges():
    nodes, edges, truncated = targets._build_graph(
        _threats(), _facts(), [("t1", "f1")], _endpoints(), _findings()
    )
    assert truncated is False
    by_id = {n.id: n for n in nodes}
    assert set(by_id) == {"threat:t1", "fact:f1", "endpoint:e1", "finding:fd1"}
    assert by_id["threat:t1"].severity == "critical"
    assert by_id["threat:t1"].status == "exploited"

    pairs = {(e.source, e.target, e.relation, e.kind) for e in edges}
    assert ("fact:f1", "threat:t1", "supports", "explicit") in pairs
    assert ("threat:t1", "endpoint:e1", "targets", "derived") in pairs
    assert ("finding:fd1", "threat:t1", "proves", "derived") in pairs


def test_build_graph_skips_unmatched_and_off_task_finding():
    threats = _threats()
    threats[0]["target_endpoint"] = "/nope"
    nodes, edges, _ = targets._build_graph(
        threats, _facts(), [("t1", "f1")], _endpoints(), _findings()
    )
    # 端点未命中 → 不产生 targets 边；但 finding 与威胁同源且文本命中 CVE，仍连 proves
    assert not any(e.relation == "targets" for e in edges)
    assert any(e.relation == "proves" for e in edges)
    assert any(n.type == "endpoint" for n in nodes) is False

    # 换一个不同任务的 finding：不再构成 proves（禁止臆造跨任务血缘）
    off = _findings()
    off[0]["task_id"] = "task-9"
    _, edges2, _ = targets._build_graph(
        _threats(), _facts(), [("t1", "f1")], _endpoints(), off
    )
    assert not any(e.relation == "proves" for e in edges2)


def test_build_graph_truncates_at_node_cap(monkeypatch):
    monkeypatch.setattr(targets, "_MAX_GRAPH_NODES", 2)
    nodes, _edges, truncated = targets._build_graph(
        _threats(), _facts(), [("t1", "f1")], _endpoints(), _findings()
    )
    assert truncated is True
    assert len(nodes) == 2


def test_build_graph_empty_inputs():
    nodes, edges, truncated = targets._build_graph([], [], [], [], [])
    assert (nodes, edges, truncated) == ([], [], False)


def test_attack_flow_out_defaults_to_empty():
    out = AttackFlowOut(target_id="x")
    assert out.graph.nodes == []
    assert out.timeline.lanes == []
    assert out.timeline.start is None


# ---------------------------------------------------------------------- 租户隔离

class _FakeUser:
    def __init__(self, tenant_id: str) -> None:
        self.id = "user-test"
        self.tenant_id = tenant_id
        self.email = "tester@example.com"
        setattr(self, "_effective_scopes", ["*"])


class _GetOnlySession:
    """只实现 get 的替身：target/task 缺失或属他租户时返回隔离结果。"""

    def __init__(self, target=None, task=None) -> None:
        self._target = target
        self._task = task

    async def get(self, model, ident):  # noqa: ANN001, ANN201
        if model.__name__ == "Target":
            return self._target
        if model.__name__ == "Task":
            return self._task
        return None


def _run(coro):
    return asyncio.run(coro)


def test_attack_flow_rejects_missing_or_foreign_target():
    tid = uuid.uuid4()

    async def scenario():
        with pytest.raises(NotFoundError):
            await targets.get_target_attack_flow(
                target_id=tid,
                session=_GetOnlySession(target=None),
                user=_FakeUser("tenant-a"),
            )
        foreign = type("Target", (), {"id": tid, "tenant_id": "tenant-b"})()
        with pytest.raises(NotFoundError):
            await targets.get_target_attack_flow(
                target_id=tid,
                session=_GetOnlySession(target=foreign),
                user=_FakeUser("tenant-a"),
            )

    _run(scenario())


def test_event_range_rejects_missing_or_foreign_task():
    tid = uuid.uuid4()

    async def scenario():
        with pytest.raises(NotFoundError):
            await persistence.get_task_event_range(
                task_id=tid,
                session=_GetOnlySession(task=None),
                user=_FakeUser("tenant-a"),
            )
        foreign = type("Task", (), {"id": tid, "tenant_id": "tenant-b"})()
        with pytest.raises(NotFoundError):
            await persistence.get_task_event_range(
                task_id=tid,
                session=_GetOnlySession(task=foreign),
                user=_FakeUser("tenant-a"),
            )

    _run(scenario())


# ---------------------------------------------------------------------- 区间契约

def test_event_range_out_seq_boundaries():
    empty = TaskEventRangeOut(task_id="t")
    assert empty.total == 0
    assert empty.min_seq is None and empty.max_seq is None

    filled = TaskEventRangeOut(
        task_id="t", total=3, min_seq=1, max_seq=7,
        first_at="2026-09-01T00:00:00+00:00", last_at="2026-09-01T00:01:00+00:00",
    )
    assert filled.max_seq - filled.min_seq + 1 >= filled.total
