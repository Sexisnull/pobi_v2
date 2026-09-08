"""Target CRUD 路由。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from pobi_agent.logging import logger
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import UUID

from pobi_v2.core.deps import get_current_user, require_scope
from pobi_v2.core.exceptions import ConflictError, NotFoundError
from pobi_v2.db.models import Artifact, Finding, Target, Task, TaskEvent, User
from pobi_v2.engine.recon_access import delete_task_local_data
from pobi_v2.db.persistence import record_audit_safe
from pobi_v2.db.recon_models import (
    ReconEndpointAgg,
    ReconFactAgg,
    ReconThreatAgg,
    ReconThreatEvidenceLink,
)
from pobi_v2.db.session import get_session
from pobi_v2.schemas.recon import (
    ReconTreeHost,
    ReconTreeNode,
    ReconTreeOut,
    TargetOverviewSummaryOut,
)
from pobi_v2.schemas.target import (
    AttackFlowGraphOut,
    AttackFlowOut,
    AttackFlowTimelineOut,
    GraphEdgeOut,
    GraphNodeOut,
    TargetCreate,
    TargetRead,
    TargetUpdate,
    TimelineBucketOut,
    TimelineLaneOut,
)

router = APIRouter(prefix="/api/v1/targets", tags=["targets"])


def _to_orm_payload(data: TargetCreate | TargetUpdate) -> dict:
    payload = data.model_dump(exclude_unset=True)
    # in_scope / out_of_scope 直接以 list 存入 JSON 列
    return payload


@router.post("", response_model=TargetRead, status_code=status.HTTP_201_CREATED)
async def create_target(
    data: TargetCreate,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_scope("targets:write")),
) -> Target:
    try:
        payload = _to_orm_payload(data)
        payload["tenant_id"] = user.tenant_id
        payload["owner_id"] = user.id
        target = Target(**payload)
        session.add(target)
        await session.commit()
        await session.refresh(target)
        await record_audit_safe(
            session, action="target.created", actor=user.email, actor_id=user.id,
            tenant_id=user.tenant_id, target_id=target.id, detail=target.url,
            meta={"changed": sorted(payload.keys())},
        )
        return target
    except ConflictError:
        raise
    except Exception as exc:  # 唯一约束等
        await session.rollback()
        if "unique" in str(exc).lower():
            raise ConflictError("该 URL 已存在")
        raise


@router.get("", response_model=list[TargetRead])
async def list_targets(
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_scope("targets:read")),
    enabled: bool | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> list[Target]:
    stmt = select(Target).where(Target.tenant_id == user.tenant_id)
    if enabled is not None:
        stmt = stmt.where(Target.enabled == enabled)
    stmt = stmt.order_by(Target.created_at.desc()).limit(limit).offset(offset)
    result = await session.execute(stmt)
    return list(result.scalars().all())


@router.get("/{target_id}", response_model=TargetRead)
async def get_target(
    target_id: UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_scope("targets:read")),
) -> Target:
    target = await session.get(Target, target_id)
    if target is None or target.tenant_id != user.tenant_id:
        raise NotFoundError("目标不存在")
    return target


@router.get("/{target_id}/assets")
async def list_target_assets(
    target_id: UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_scope("targets:read")),
) -> dict:
    """目标资产/端点全景（per-target 聚合，来自 recon_endpoints_agg）。

    供前端"目标全景图"的资产清单视图：主机 + 路径 + 方法 + 状态码 + 技术栈指纹 +
    参数 + 发现途径 + 时间。跨任务收敛，同一端点跨任务只保留最新状态。
    """
    target = await session.get(Target, target_id)
    if target is None or target.tenant_id != user.tenant_id:
        raise NotFoundError("目标不存在")
    stmt = (
        select(ReconEndpointAgg)
        .where(ReconEndpointAgg.target_id == target_id)
        .order_by(ReconEndpointAgg.host, ReconEndpointAgg.path_normalized)
        .limit(1000)
    )
    result = await session.execute(stmt)
    assets = []
    for r in result.scalars().all():
        assets.append(
            {
                "id": str(r.id),
                "host": r.host,
                "path": r.path_normalized,
                "method": r.method,
                "status_code": r.status_code,
                "auth_required": r.auth_required,
                "tech_stack": r.tech_stack or [],
                "parameters": r.parameters or [],
                "discovered_via": r.discovered_via,
                "confidence": r.confidence,
                "first_seen": r.first_seen.isoformat() if r.first_seen else None,
                "last_seen": r.last_seen.isoformat() if r.last_seen else None,
            }
        )
    return {"target_id": str(target_id), "count": len(assets), "assets": assets}


# ----------------------------------------------------------------------
# 目标总览：per-target 跨任务全阶段只读聚合
# ----------------------------------------------------------------------
# 数据源分两类：recon 聚合层（facts/endpoints/threats，均带 target_id + tenant_id）
# 与通用阶段表（findings/artifacts/tasks，均带 target_id）。两类都在接口首行完成
# Target.tenant_id 校验，故查询只需 WHERE target_id = :tid，无需再 JOIN Task。

_SEVERITY_RANK = {"info": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}
_SEVERITY_NAME = {rank: name for name, rank in _SEVERITY_RANK.items()}

# 聚合层与阶段表均为 per-target 收敛数据，行数不随任务数增长，取上限即可。
_MAX_ROWS = 500

# 攻击流：时间轴泳道数与每泳道事件骨架上限（事件仅取时间/类型，不取 payload）。
_MAX_LANES = 20
_MAX_LANE_EVENTS = 20000
# 攻击流：图谱节点上限。端点可达数百行，全量渲染既不可读也会拖垮布局。
_MAX_GRAPH_NODES = 300
_DEFAULT_BUCKETS = 240
# 桶宽阶梯（毫秒）：保证不同时长的目标都能落在可读的刻度上。
_BUCKET_STEPS_MS = (
    1_000, 5_000, 10_000, 30_000, 60_000,
    300_000, 900_000, 3_600_000, 21_600_000, 86_400_000,
)


def _severity_rank(column: Any) -> Any:
    """把严重度文本/枚举列映射为可比较等级，供 func.max 取全局峰值。"""
    return case(
        *[(column == name, rank) for name, rank in _SEVERITY_RANK.items()],
        else_=0,
    )


def _rank_of(severity: Any) -> int:
    if isinstance(severity, Enum):
        severity = severity.value
    return _SEVERITY_RANK.get(str(severity or "").lower(), 0)


async def _get_target_or_404(
    session: AsyncSession, target_id: UUID, tenant_id: UUID
) -> Target:
    target = await session.get(Target, target_id)
    if target is None or target.tenant_id != tenant_id:
        raise NotFoundError("目标不存在")
    return target


async def _count_and_last(
    session: AsyncSession, model: Any, ts_column: Any, target_id: UUID
) -> tuple[int, Any]:
    """返回该目标在某表下的行数与最新时间戳。"""
    row = (
        await session.execute(
            select(func.count(), func.max(ts_column)).where(model.target_id == target_id)
        )
    ).one()
    return int(row[0]), row[1]


@router.get("/{target_id}/overview", response_model=TargetOverviewSummaryOut)
async def get_target_overview(
    target_id: UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_scope("targets:read")),
) -> TargetOverviewSummaryOut:
    """目标总览统计卡：侦察事实/端点/威胁 + 利用验证 findings + 任务数 + 峰值。"""
    await _get_target_or_404(session, target_id, user.tenant_id)
    facts_count, facts_ts = await _count_and_last(
        session, ReconFactAgg, ReconFactAgg.last_seen, target_id
    )
    endpoints_count, endpoints_ts = await _count_and_last(
        session, ReconEndpointAgg, ReconEndpointAgg.last_seen, target_id
    )
    threats_count, threats_ts = await _count_and_last(
        session, ReconThreatAgg, ReconThreatAgg.last_seen, target_id
    )
    findings_count, findings_ts = await _count_and_last(
        session, Finding, Finding.created_at, target_id
    )
    artifacts_count, artifacts_ts = await _count_and_last(
        session, Artifact, Artifact.created_at, target_id
    )
    tasks_count, tasks_ts = await _count_and_last(
        session, Task, Task.created_at, target_id
    )
    threat_rank = await session.scalar(
        select(func.max(_severity_rank(ReconThreatAgg.severity))).where(
            ReconThreatAgg.target_id == target_id
        )
    )
    finding_rank = await session.scalar(
        select(func.max(_severity_rank(Finding.severity))).where(
            Finding.target_id == target_id
        )
    )
    stamps = [
        ts
        for ts in (
            facts_ts,
            threats_ts,
            endpoints_ts,
            findings_ts,
            artifacts_ts,
            tasks_ts,
        )
        if ts is not None
    ]
    return TargetOverviewSummaryOut(
        facts_count=facts_count,
        endpoints_count=endpoints_count,
        threats_count=threats_count,
        findings_count=findings_count,
        tasks_count=tasks_count,
        severity_max=_SEVERITY_NAME.get(max(threat_rank or 0, finding_rank or 0), "info"),
        last_seen=max(stamps).isoformat() if stamps else None,
    )


@router.get("/{target_id}/tree", response_model=ReconTreeOut)
async def get_target_tree(
    target_id: UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_scope("targets:read")),
) -> ReconTreeOut:
    """目标 Web 页面树：endpoints_agg 按 host 分组，叶子挂载命中的最高威胁严重度。

    威胁表无端点外键，target_endpoint 为自由字符串，故按路径匹配：优先全等，
    回退子串（根路径 "/" 过宽泛，仅参与全等匹配）。
    """
    await _get_target_or_404(session, target_id, user.tenant_id)
    endpoints = (
        await session.scalars(
            select(ReconEndpointAgg)
            .where(ReconEndpointAgg.target_id == target_id)
            .order_by(ReconEndpointAgg.host, ReconEndpointAgg.path_normalized)
            .limit(_MAX_ROWS)
        )
    ).all()
    threat_rows = (
        await session.execute(
            select(
                ReconThreatAgg.target_endpoint,
                ReconThreatAgg.severity,
                ReconThreatAgg.confidence,
            ).where(ReconThreatAgg.target_id == target_id)
        )
    ).all()
    threats = [(r[0] or "", _rank_of(r[1]), r[2] or 0.0) for r in threat_rows]

    hosts: dict[str, list[ReconTreeNode]] = {}
    for ep in endpoints:
        path = ep.path_normalized or ""
        matched = [t for t in threats if t[0] == path]
        if not matched and len(path) > 1:
            matched = [t for t in threats if t[0] and (t[0] in path or path in t[0])]
        if matched:
            top = max(t[1] for t in matched)
            severity = _SEVERITY_NAME.get(top, "info")
            confidence = max(t[2] for t in matched if t[1] == top)
        else:
            severity, confidence = "info", 0.0
        hosts.setdefault(ep.host or "", []).append(
            ReconTreeNode(
                path=path,
                method=ep.method,
                status_code=ep.status_code,
                auth_required=ep.auth_required,
                tech_stack=list(ep.tech_stack or []),
                threat_severity_max=severity,
                threat_confidence=confidence,
            )
        )
    return ReconTreeOut(
        hosts=[ReconTreeHost(host=host, paths=paths) for host, paths in sorted(hosts.items())],
        total=sum(len(paths) for paths in hosts.values()),
    )


# ----------------------------------------------------------------------
# 攻击流（Attack Flow）：关系图谱 + 事件时间轴
# ----------------------------------------------------------------------
# 事件明细量级可达数十万行，故时间轴在后端按「任务泳道 × 时间桶」聚合计数，
# 只回传轻量列（不取 payload）；图谱只取威胁连通子图，端点全量展开为显式开关。


def _epoch_ms(ts: Any) -> float:
    """datetime → epoch 毫秒（缺失时区按 UTC 处理，PG 侧均为 timestamptz）。"""
    if ts is None:
        return 0.0
    dt = ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    return dt.timestamp() * 1000.0


def _iso_from_ms(ms: float) -> str:
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).isoformat()


def _event_class(event_type: str) -> str:
    """事件类型 → 大类：recon/tool/finding/error/other。"""
    t = (event_type or "").lower()
    if "error" in t or "fail" in t:
        return "error"
    if t.startswith("tool_"):
        return "tool"
    if t.startswith("recon_") or t in {"phase_changed", "plan_step"}:
        return "recon"
    if t in {"validation_result", "report_task_event"} or "finding" in t:
        return "finding"
    return "other"


def _choose_bucket_ms(start_ms: float, end_ms: float, max_buckets: int) -> int:
    """按目标整体时间跨度选出阶梯桶宽，使桶数不超过 max_buckets。"""
    span = end_ms - start_ms
    if span <= 0:
        return _BUCKET_STEPS_MS[0]
    raw = span / max(1, max_buckets)
    for step in _BUCKET_STEPS_MS:
        if raw <= step:
            return step
    return _BUCKET_STEPS_MS[-1]


def _bucket_events(points: list[tuple[float, str]], start_ms: float, bucket_ms: int) -> list[dict]:
    """把 (时间, 大类) 点集压成时间桶，只输出非空桶。"""
    acc: dict[int, dict[str, int]] = {}
    for ts, cls in points:
        idx = max(0, int((ts - start_ms) // bucket_ms)) if bucket_ms > 0 else 0
        counts = acc.setdefault(idx, {})
        counts[cls] = counts.get(cls, 0) + 1
    return [
        {
            "index": idx,
            "start": _iso_from_ms(start_ms + idx * bucket_ms),
            "end": _iso_from_ms(start_ms + (idx + 1) * bucket_ms),
            "counts": acc[idx],
        }
        for idx in sorted(acc)
    ]


def _match_paths(path: str, target: str) -> bool:
    """威胁 target_endpoint 与端点路径的匹配规则（与 /tree 口径一致）。

    威胁表无端点外键，target_endpoint 为自由字符串：全等优先，回退子串；
    根路径 "/" 过宽泛，仅参与全等匹配。
    """
    if not path or not target:
        return False
    if path == target:
        return True
    if len(path) <= 1 or len(target) <= 1:
        return False
    return target in path or path in target


def _build_graph(
    threats: list[dict],
    facts: list[dict],
    links: list[tuple[str, str]],
    endpoints: list[dict],
    findings: list[dict],
    full_endpoints: bool = False,
) -> tuple[list[GraphNodeOut], list[GraphEdgeOut], bool]:
    """组装攻击路径子图：事实 →(supports) 威胁 →(targets) 端点，发现 →(proves) 威胁。

    入参为纯字典，便于单测；节点按「威胁优先、高严重度优先」进入，超出
    _MAX_GRAPH_NODES 后停止扩展并置 truncated。
    """
    nodes: list[GraphNodeOut] = []
    edges: list[GraphEdgeOut] = []
    seen: set[str] = set()
    truncated = False

    def add(node: GraphNodeOut) -> bool:
        nonlocal truncated
        if node.id in seen:
            return True
        if len(nodes) >= _MAX_GRAPH_NODES:
            truncated = True
            return False
        seen.add(node.id)
        nodes.append(node)
        return True

    fact_by_id = {str(f["id"]): f for f in facts}

    for t in sorted(threats, key=lambda x: -_rank_of(x.get("severity"))):
        add(
            GraphNodeOut(
                id=f"threat:{t['id']}",
                type="threat",
                label=t.get("title") or t.get("cve_id") or t.get("category") or "威胁",
                severity=str(t.get("severity") or "info").lower(),
                status=t.get("status"),
                confidence=float(t.get("confidence") or 0.0),
                source_tasks=[str(x) for x in (t.get("source_tasks") or [])],
                detail={
                    "cve_id": t.get("cve_id") or "",
                    "category": t.get("category") or "",
                    "cvss_score": t.get("cvss_score"),
                    "target_endpoint": t.get("target_endpoint") or "",
                    "evidence_summary": t.get("evidence_summary") or "",
                },
            )
        )

    # 事实：只纳入被证据关联引用的（威胁子图聚焦，避免全量事实噪声）
    for threat_id, fact_id in links:
        tid = f"threat:{threat_id}"
        if tid not in seen:
            continue
        fact = fact_by_id.get(str(fact_id))
        if not fact:
            continue
        fid = f"fact:{fact['id']}"
        if not add(
            GraphNodeOut(
                id=fid,
                type="fact",
                label=f"{fact.get('category') or 'fact'}:{fact.get('key') or ''}",
                confidence=float(fact.get("confidence") or 0.0),
                source_tasks=[str(x) for x in (fact.get("source_tasks") or [])],
                detail={
                    "category": fact.get("category") or "",
                    "key": fact.get("key") or "",
                    "value": fact.get("value") or "",
                },
            )
        ):
            continue
        edges.append(
            GraphEdgeOut(source=fid, target=tid, relation="supports", kind="explicit")
        )

    # 端点：默认只挂载被威胁 target_endpoint 命中的；full_endpoints 时全量纳入
    endpoint_added: set[str] = set()
    for t in threats:
        tid = f"threat:{t['id']}"
        if tid not in seen:
            continue
        target_ep = (t.get("target_endpoint") or "").strip()
        if not target_ep:
            continue
        for ep in endpoints:
            if not _match_paths(ep.get("path") or "", target_ep):
                continue
            eid = f"endpoint:{ep['id']}"
            if eid not in endpoint_added and add(
                GraphNodeOut(
                    id=eid,
                    type="endpoint",
                    label=f"{ep.get('method') or 'GET'} {ep.get('host') or ''}{ep.get('path') or ''}",
                    confidence=float(ep.get("confidence") or 0.0),
                    source_tasks=[str(x) for x in (ep.get("source_tasks") or [])],
                    detail={
                        "host": ep.get("host") or "",
                        "path": ep.get("path") or "",
                        "method": ep.get("method") or "GET",
                        "status_code": ep.get("status_code"),
                        "auth_required": bool(ep.get("auth_required")),
                        "tech_stack": list(ep.get("tech_stack") or []),
                    },
                )
            ):
                endpoint_added.add(eid)
            if eid in seen:
                edges.append(
                    GraphEdgeOut(source=tid, target=eid, relation="targets", kind="derived")
                )

    if full_endpoints:
        for ep in endpoints:
            eid = f"endpoint:{ep['id']}"
            if eid in seen:
                continue
            add(
                GraphNodeOut(
                    id=eid,
                    type="endpoint",
                    label=f"{ep.get('method') or 'GET'} {ep.get('host') or ''}{ep.get('path') or ''}",
                    confidence=float(ep.get("confidence") or 0.0),
                    source_tasks=[str(x) for x in (ep.get("source_tasks") or [])],
                    detail={
                        "host": ep.get("host") or "",
                        "path": ep.get("path") or "",
                        "method": ep.get("method") or "GET",
                        "status_code": ep.get("status_code"),
                        "auth_required": bool(ep.get("auth_required")),
                        "tech_stack": list(ep.get("tech_stack") or []),
                    },
                )
            )

    # 发现：与威胁同源任务且文本命中 CVE/类别才连 proves 边，避免臆造血缘
    for fd in findings:
        fid = f"finding:{fd['id']}"
        if not add(
            GraphNodeOut(
                id=fid,
                type="finding",
                label=fd.get("title") or "漏洞发现",
                severity=str(fd.get("severity") or "info").lower(),
                confidence=float(fd.get("confidence") or 0.0),
                source_tasks=[str(fd["task_id"])] if fd.get("task_id") else [],
                detail={
                    "cwe": fd.get("cwe") or "",
                    "description": fd.get("description") or "",
                    "task_id": str(fd["task_id"]) if fd.get("task_id") else "",
                },
            )
        ):
            continue
        text = " ".join(
            str(fd.get(k) or "") for k in ("title", "cwe", "description")
        ).lower()
        if not text.strip():
            continue
        for t in threats:
            tid = f"threat:{t['id']}"
            if tid not in seen:
                continue
            if str(fd.get("task_id")) not in {str(x) for x in (t.get("source_tasks") or [])}:
                continue
            cve = (t.get("cve_id") or "").strip().lower()
            category = (t.get("category") or "").strip().lower()
            if (cve and cve in text) or (category and category in text):
                edges.append(
                    GraphEdgeOut(source=fid, target=tid, relation="proves", kind="derived")
                )

    return nodes, edges, truncated


@router.get("/{target_id}/attack-flow", response_model=AttackFlowOut)
async def get_target_attack_flow(
    target_id: UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_scope("targets:read")),
    full_endpoints: bool = Query(
        default=False, description="展开全部端点（默认仅威胁连通子图）"
    ),
    buckets: int = Query(default=_DEFAULT_BUCKETS, ge=20, le=300),
) -> AttackFlowOut:
    """目标攻击流：关系图谱（因果结构）+ 任务泳道事件密度时间轴（执行节奏）。

    图谱的边全部来自现成关联：``recon_threat_evidence_link``（显式证据）与
    ``threat.target_endpoint`` → ``endpoint.path_normalized`` 的路径匹配（与
    /tree 同口径）。时间轴只查 TaskEvent 的时间/类型列（不取 payload），按任务
    分泳道、固定桶数聚合计数，前端渲染量与事件总量无关。
    """
    await _get_target_or_404(session, target_id, user.tenant_id)

    # ---- 图谱数据源
    threat_rows = (
        await session.scalars(
            select(ReconThreatAgg)
            .where(ReconThreatAgg.target_id == target_id)
            .order_by(ReconThreatAgg.confidence.desc())
            .limit(_MAX_ROWS)
        )
    ).all()
    threats = [
        {
            "id": str(r.id),
            "cve_id": r.cve_id,
            "title": r.title,
            "category": r.category,
            "severity": r.severity,
            "status": r.status,
            "cvss_score": r.cvss_score,
            "target_endpoint": r.target_endpoint,
            "evidence_summary": r.evidence_summary,
            "confidence": r.confidence,
            "source_tasks": r.source_tasks or [],
        }
        for r in threat_rows
    ]

    links = [
        (str(tid), str(fid))
        for tid, fid in (
            await session.execute(
                select(
                    ReconThreatEvidenceLink.threat_agg_id,
                    ReconThreatEvidenceLink.fact_agg_id,
                )
                .join(ReconThreatAgg, ReconThreatAgg.id == ReconThreatEvidenceLink.threat_agg_id)
                .where(ReconThreatAgg.target_id == target_id)
            )
        ).all()
    ]
    fact_ids = {fid for _, fid in links}
    facts = []
    if fact_ids:
        fact_rows = (
            await session.scalars(
                select(ReconFactAgg).where(
                    ReconFactAgg.target_id == target_id,
                    ReconFactAgg.id.in_([UUID(i) for i in fact_ids]),
                )
            )
        ).all()
        facts = [
            {
                "id": str(r.id),
                "category": r.category,
                "key": r.key,
                "value": r.value,
                "confidence": r.confidence,
                "source_tasks": r.source_tasks or [],
            }
            for r in fact_rows
        ]

    endpoint_rows = (
        await session.scalars(
            select(ReconEndpointAgg)
            .where(ReconEndpointAgg.target_id == target_id)
            .order_by(ReconEndpointAgg.host, ReconEndpointAgg.path_normalized)
            .limit(_MAX_ROWS)
        )
    ).all()
    endpoints = [
        {
            "id": str(r.id),
            "host": r.host,
            "path": r.path_normalized,
            "method": r.method,
            "status_code": r.status_code,
            "auth_required": r.auth_required,
            "tech_stack": r.tech_stack or [],
            "confidence": r.confidence,
            "source_tasks": r.source_tasks or [],
        }
        for r in endpoint_rows
    ]

    finding_rows = (
        await session.scalars(
            select(Finding)
            .where(Finding.target_id == target_id)
            .order_by(Finding.created_at.desc())
            .limit(_MAX_ROWS)
        )
    ).all()
    findings = [
        {
            "id": str(r.id),
            "task_id": str(r.task_id),
            "title": r.title,
            "severity": r.severity.value if isinstance(r.severity, Enum) else r.severity,
            "cwe": r.cwe,
            "description": r.description,
            "confidence": r.confidence,
        }
        for r in finding_rows
    ]

    nodes, edges, graph_truncated = _build_graph(
        threats, facts, links, endpoints, findings, full_endpoints
    )

    # ---- 时间轴数据源（按任务泳道逐条取轻量列，避免一次性拉全量 payload）
    task_rows = (
        await session.execute(
            select(Task.id, Task.name, Task.status, Task.started_at, Task.finished_at)
            .where(Task.target_id == target_id)
            .order_by(Task.created_at.desc())
            .limit(_MAX_LANES)
        )
    ).all()

    points_by_task: dict[str, list[tuple[float, str]]] = {}
    lane_truncated: dict[str, bool] = {}
    timeline_truncated = len(task_rows) >= _MAX_LANES
    for tid, _name, _status, _started, _finished in task_rows:
        event_rows = (
            await session.execute(
                select(TaskEvent.created_at, TaskEvent.event_type)
                .where(TaskEvent.task_id == tid)
                .order_by(TaskEvent.seq)
                .limit(_MAX_LANE_EVENTS)
            )
        ).all()
        lane_truncated[str(tid)] = len(event_rows) >= _MAX_LANE_EVENTS
        timeline_truncated = timeline_truncated or lane_truncated[str(tid)]
        points_by_task[str(tid)] = [
            (_epoch_ms(ts), _event_class(et)) for ts, et in event_rows if ts is not None
        ]

    all_ts = [p[0] for pts in points_by_task.values() for p in pts]
    lanes: list[TimelineLaneOut] = []
    start = end = None
    bucket_ms = 0
    if all_ts:
        start_ms, end_ms = min(all_ts), max(all_ts)
        bucket_ms = _choose_bucket_ms(start_ms, end_ms, buckets)
        start, end = _iso_from_ms(start_ms), _iso_from_ms(end_ms)
        for tid, name, status, started_at, finished_at in task_rows:
            points = points_by_task.get(str(tid), [])
            lanes.append(
                TimelineLaneOut(
                    task_id=str(tid),
                    name=name,
                    status=status.value if isinstance(status, Enum) else str(status),
                    started_at=started_at.isoformat() if started_at else None,
                    finished_at=finished_at.isoformat() if finished_at else None,
                    total=len(points),
                    truncated=lane_truncated.get(str(tid), False),
                    buckets=[
                        TimelineBucketOut(**b)
                        for b in _bucket_events(points, start_ms, bucket_ms)
                    ],
                )
            )

    return AttackFlowOut(
        target_id=str(target_id),
        graph=AttackFlowGraphOut(nodes=nodes, edges=edges, truncated=graph_truncated),
        timeline=AttackFlowTimelineOut(
            start=start,
            end=end,
            bucket_ms=bucket_ms,
            truncated=timeline_truncated,
            lanes=lanes,
        ),
    )


@router.get("/{target_id}/facts")
async def list_target_facts(
    target_id: UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_scope("targets:read")),
    limit: int = Query(default=200, ge=1, le=_MAX_ROWS),
) -> dict:
    """目标侦察事实（跨任务收敛，唯一键 target_id+category+key）。"""
    await _get_target_or_404(session, target_id, user.tenant_id)
    rows = (
        await session.scalars(
            select(ReconFactAgg)
            .where(ReconFactAgg.target_id == target_id)
            .order_by(ReconFactAgg.category, ReconFactAgg.key)
            .limit(limit)
        )
    ).all()
    items = [
        {
            "id": str(r.id),
            "category": r.category,
            "key": r.key,
            "value": r.value,
            "confidence": r.confidence,
            "source_tasks": [str(t) for t in (r.source_tasks or [])],
            "first_seen": r.first_seen.isoformat() if r.first_seen else None,
            "last_seen": r.last_seen.isoformat() if r.last_seen else None,
        }
        for r in rows
    ]
    return {"target_id": str(target_id), "total": len(items), "items": items}


@router.get("/{target_id}/threats")
async def list_target_threats(
    target_id: UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_scope("targets:read")),
    limit: int = Query(default=200, ge=1, le=_MAX_ROWS),
) -> dict:
    """目标侦察威胁（severity 降序，含 CVE/CVSS/证据摘要）。"""
    await _get_target_or_404(session, target_id, user.tenant_id)
    rows = (
        await session.scalars(
            select(ReconThreatAgg)
            .where(ReconThreatAgg.target_id == target_id)
            .order_by(ReconThreatAgg.confidence.desc())
            .limit(limit)
        )
    ).all()
    items = [
        {
            "id": str(r.id),
            "cve_id": r.cve_id,
            "title": r.title,
            "category": r.category,
            "severity": r.severity,
            "status": r.status,
            "cvss_score": r.cvss_score,
            "target_endpoint": r.target_endpoint,
            "evidence_summary": r.evidence_summary,
            "confidence": r.confidence,
            "first_seen": r.first_seen.isoformat() if r.first_seen else None,
            "last_seen": r.last_seen.isoformat() if r.last_seen else None,
        }
        for r in rows
    ]
    return {"target_id": str(target_id), "total": len(items), "items": items}


@router.get("/{target_id}/findings")
async def list_target_findings(
    target_id: UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_scope("targets:read")),
    limit: int = Query(default=200, ge=1, le=_MAX_ROWS),
) -> dict:
    """目标利用验证结果（跨任务聚合的 findings，含 CWE 与置信度）。"""
    await _get_target_or_404(session, target_id, user.tenant_id)
    rows = (
        await session.scalars(
            select(Finding)
            .where(Finding.target_id == target_id)
            .order_by(Finding.created_at.desc())
            .limit(limit)
        )
    ).all()
    items = [
        {
            "id": str(r.id),
            "task_id": str(r.task_id),
            "title": r.title,
            "severity": r.severity.value if isinstance(r.severity, Enum) else r.severity,
            "cwe": r.cwe,
            "confidence": r.confidence,
            "description": r.description,
            "evidence": r.evidence or {},
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows
    ]
    return {"target_id": str(target_id), "total": len(items), "items": items}


@router.get("/{target_id}/artifacts")
async def list_target_artifacts(
    target_id: UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_scope("targets:read")),
    limit: int = Query(default=200, ge=1, le=_MAX_ROWS),
) -> dict:
    """目标产物清单（截图/PoC/报告/日志元数据，实体内容在对象存储）。"""
    await _get_target_or_404(session, target_id, user.tenant_id)
    rows = (
        await session.scalars(
            select(Artifact)
            .where(Artifact.target_id == target_id)
            .order_by(Artifact.created_at.desc())
            .limit(limit)
        )
    ).all()
    items = [
        {
            "id": str(r.id),
            "task_id": str(r.task_id),
            "finding_id": str(r.finding_id) if r.finding_id else None,
            "kind": r.kind.value if isinstance(r.kind, Enum) else r.kind,
            "name": r.name,
            "content_type": r.content_type,
            "size_bytes": r.size_bytes,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows
    ]
    return {"target_id": str(target_id), "total": len(items), "items": items}


@router.patch("/{target_id}", response_model=TargetRead)
async def update_target(
    target_id: UUID,
    data: TargetUpdate,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_scope("targets:write")),
) -> Target:
    target = await session.get(Target, target_id)
    if target is None or target.tenant_id != user.tenant_id:
        raise NotFoundError("目标不存在")
    for key, value in _to_orm_payload(data).items():
        setattr(target, key, value)
    await session.commit()
    await session.refresh(target)
    await record_audit_safe(
        session, action="target.updated", actor=user.email, actor_id=user.id,
        tenant_id=user.tenant_id, target_id=target.id, detail=target.url,
        meta={"changed": sorted(data.model_dump(exclude_unset=True).keys())},
    )
    return target


@router.delete("/{target_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_target(
    target_id: UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_scope("targets:write")),
) -> None:
    target = await session.get(Target, target_id, options=[joinedload(Target.tasks)])
    if target is None or target.tenant_id != user.tenant_id:
        raise NotFoundError("目标不存在")
    # 先取任务 id：级联删除后 ORM 会清空关系，无法再取。
    task_ids = [t.id for t in target.tasks]
    target_url = target.url
    await session.delete(target)
    await session.commit()
    # 目标删除后 audit.target_id 会被置空（FK SET NULL），故 URL 与任务数写入 detail/meta
    await record_audit_safe(
        session, action="target.deleted", actor=user.email, actor_id=user.id,
        tenant_id=user.tenant_id, detail=target_url,
        meta={"deleted_tasks": len(task_ids)},
    )

    # 清理各任务的本地缓存目录（TASKS_ROOT/<task_id>/）。
    # 本地目录按 task_id 组织，不会随目标删除自动消失，须逐个清理，
    # 否则与单任务删除（delete_task）不一致，遗留孤儿目录。
    # 失败不影响 DB 记录已删除的结果。
    for task_id in task_ids:
        try:
            delete_task_local_data(task_id)
        except Exception:  # noqa: BLE001
            logger.exception("删除目标 %s 时清理任务 %s 本地缓存目录失败", target_id, task_id)
