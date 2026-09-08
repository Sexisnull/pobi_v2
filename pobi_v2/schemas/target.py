"""Target 的 Pydantic Schema。"""
from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field


class ScopeList(BaseModel):
    in_scope: list[str] = Field(default_factory=list)
    out_of_scope: list[str] = Field(default_factory=list)


class TargetCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    url: str = Field(..., min_length=1, max_length=2048)
    description: str | None = None
    in_scope: list[str] = Field(default_factory=list)
    out_of_scope: list[str] = Field(default_factory=list)
    # 验证策略（Validation Configuration）
    flag_regex: str | None = Field(default=None, max_length=512)
    validation_format: str | None = Field(default=None, max_length=64)
    confidence_threshold: float = Field(default=0.6, ge=0.0, le=1.0)
    max_tree_depth: int = Field(default=4, ge=1, le=16)
    enabled: bool = True


class TargetUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=255)
    url: str | None = Field(default=None, max_length=2048)
    description: str | None = None
    in_scope: list[str] | None = None
    out_of_scope: list[str] | None = None
    flag_regex: str | None = Field(default=None, max_length=512)
    validation_format: str | None = Field(default=None, max_length=64)
    confidence_threshold: float | None = Field(default=None, ge=0.0, le=1.0)
    max_tree_depth: int | None = Field(default=None, ge=1, le=16)
    enabled: bool | None = None


class TargetRead(BaseModel):
    id: UUID
    name: str
    url: str
    description: str | None
    in_scope: list[str]
    out_of_scope: list[str]
    flag_regex: str | None
    validation_format: str | None
    confidence_threshold: float
    max_tree_depth: int
    enabled: bool
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


# ----------------------------------------------------------------------
# 攻击流（Attack Flow）：目标级「关系图谱 + 事件时间轴」聚合出参
# ----------------------------------------------------------------------
# 时间轴只回传时间桶计数（不回传事件明细），图谱只回传连通子图的节点与边，
# 两者均由后端聚合，前端只做渲染，故数据量与目标事件总量解耦。


class GraphNodeOut(BaseModel):
    """图谱节点：threat / fact / endpoint / finding 四类之一。

    detail 承载各类型特有字段（CVE/CVSS/证据摘要/主机/参数等），供右侧详情抽屉
    直接渲染，避免前端再按类型二次请求。
    """

    id: str
    type: str
    label: str
    severity: str = "info"
    status: str | None = None
    confidence: float = 0.0
    source_tasks: list[str] = Field(default_factory=list)
    detail: dict[str, Any] = Field(default_factory=dict)


class GraphEdgeOut(BaseModel):
    """图谱边：supports（现成证据关联）/ targets（端点匹配）/ proves（发现反证威胁）。"""

    source: str
    target: str
    relation: str
    kind: str = "derived"


class AttackFlowGraphOut(BaseModel):
    nodes: list[GraphNodeOut] = Field(default_factory=list)
    edges: list[GraphEdgeOut] = Field(default_factory=list)
    truncated: bool = False


class TimelineBucketOut(BaseModel):
    """一个时间桶：仅非空桶才回传，空档即"空转区间"，由前端留白呈现。"""

    index: int
    start: str
    end: str
    counts: dict[str, int] = Field(default_factory=dict)


class TimelineLaneOut(BaseModel):
    """一个任务泳道：桶序列共享全局时间轴起点与桶宽。"""

    task_id: str
    name: str
    status: str
    started_at: str | None = None
    finished_at: str | None = None
    total: int = 0
    truncated: bool = False
    buckets: list[TimelineBucketOut] = Field(default_factory=list)


class AttackFlowTimelineOut(BaseModel):
    start: str | None = None
    end: str | None = None
    bucket_ms: int = 0
    truncated: bool = False
    lanes: list[TimelineLaneOut] = Field(default_factory=list)


class AttackFlowOut(BaseModel):
    target_id: str
    graph: AttackFlowGraphOut = Field(default_factory=AttackFlowGraphOut)
    timeline: AttackFlowTimelineOut = Field(default_factory=AttackFlowTimelineOut)
