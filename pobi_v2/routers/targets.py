"""Target CRUD 路由。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from pobi_agent.logging import logger
from enum import Enum
from typing import Any
from uuid import UUID

from pobi_v2.core.deps import get_current_user, require_scope
from pobi_v2.core.exceptions import ConflictError, NotFoundError
from pobi_v2.db.models import Artifact, Finding, Target, Task, User
from pobi_v2.engine.recon_access import delete_task_local_data
from pobi_v2.db.persistence import record_audit_safe
from pobi_v2.db.recon_models import ReconEndpointAgg, ReconFactAgg, ReconThreatAgg
from pobi_v2.db.session import get_session
from pobi_v2.schemas.recon import (
    ReconTreeHost,
    ReconTreeNode,
    ReconTreeOut,
    TargetOverviewSummaryOut,
)
from pobi_v2.schemas.target import TargetCreate, TargetRead, TargetUpdate

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
