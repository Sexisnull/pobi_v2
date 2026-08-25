"""RECON 任务侦察数据查询响应 Schema（供前端展示）。

字段严格对齐 ``pobi_agent.recon.store.ReconStore`` 只读查询面返回结构：
get_summary / list_facts / list_endpoints / list_threats / get_threat /
derived_assets。所有计数字段设置默认值，保证本地库缺失时返回空结构而非 5xx。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class ReconSummaryOut(BaseModel):
    """任务侦察总览：资产/事实/终端/技术/威胁计数 + 威胁状态与严重度分布。"""

    assets: Dict[str, int] = Field(default_factory=lambda: {"hosts": 0, "services": 0})
    facts_count: int = 0
    endpoints_count: int = 0
    techniques_count: int = 0
    threats_count: int = 0
    threats_by_status: Dict[str, int] = Field(
        default_factory=lambda: {
            "suspected": 0,
            "confirmed": 0,
            "exploited": 0,
            "remediated": 0,
        }
    )
    threats_by_severity: Dict[str, int] = Field(
        default_factory=lambda: {
            "critical": 0,
            "high": 0,
            "medium": 0,
            "low": 0,
            "info": 0,
        }
    )


class ReconFactOut(BaseModel):
    id: int
    category: str = ""
    key: str = ""
    value: str = ""
    confidence: float = 0.0
    source: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class ReconEndpointOut(BaseModel):
    id: int
    host: Optional[str] = None
    path: str = ""
    method: Optional[str] = None
    status_code: Optional[int] = None
    auth_required: bool = False
    tech_stack: List[str] = Field(default_factory=list)
    confidence: float = 0.0


class ReconThreatOut(BaseModel):
    id: int
    cve_id: str = ""
    title: str = ""
    category: str = ""
    severity: str = "info"
    status: str = "suspected"
    cvss_score: Optional[float] = None
    target_endpoint: str = ""
    evidence_summary: str = ""
    confidence: float = 0.0
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class ReconHostOut(BaseModel):
    host: str = ""
    endpoint_count: int = 0
    tech_stack: List[str] = Field(default_factory=list)
    auth_endpoints: int = 0
    confidence: float = 0.0


class ReconServiceOut(BaseModel):
    host: Optional[str] = None
    path: str = ""
    method: Optional[str] = None
    status_code: Optional[int] = None
    tech_stack: List[str] = Field(default_factory=list)
    confidence: float = 0.0


class ReconAssetsOut(BaseModel):
    """派生资产视图：主机清单 + 服务/端口/子域类资产。"""

    hosts: List[ReconHostOut] = Field(default_factory=list)
    services: List[ReconServiceOut] = Field(default_factory=list)


class ReconCoverageOut(BaseModel):
    """增量续扫基线（已覆盖资产，历史任务沉淀，本轮跳过重复工作）。"""

    covered_endpoints: List[str] = Field(default_factory=list)
    covered_techniques: List[str] = Field(default_factory=list)
    covered_threats: List[str] = Field(default_factory=list)
    already_covered_count: int = 0


class ReconListEnvelope(BaseModel):
    """列表接口统一信封，便于前端分页/空态处理。"""

    items: List[Any] = Field(default_factory=list)
    total: int = 0
