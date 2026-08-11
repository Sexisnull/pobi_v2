"""M5 审批引擎：高危操作的审批请求与决策。

设计：
- 纯逻辑 ``evaluate_tool_call``：依据高危工具名单判定某次工具调用是否需审批。
- 持久化 ``create_approval_request`` / ``decide_request``：审批请求状态机
  （pending -> approved/rejected）。
- ``make_approval_callback``：返回兼容 pobi_agent ``PobiAgent.set_approval_callback``
  的异步回调——运行时若需审批，回调轮询 DB 决策，超时默认拒绝（fail-closed）。

离线测试可直接验证 ``evaluate_tool_call`` 与决策状态机逻辑。
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from uuid import UUID

from pobi_v2.db.models import ApprovalRequest, ApprovalStatus

_DEFAULT_HIGH_RISK_TOOLS = {
    "execute_command",
    "run_shell",
    "shell",
    "reverse_shell",
    "write_file",
    "exfiltrate",
    "exploit",
    "sql_injection",
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def evaluate_tool_call(tool_name: str, high_risk_tools=None) -> bool:
    """判定工具调用是否需要审批（不区分大小写匹配高危集合）。"""
    tools = high_risk_tools or _DEFAULT_HIGH_RISK_TOOLS
    return tool_name.lower() in {t.lower() for t in tools}


async def create_approval_request(
    session,
    task_id: UUID,
    tenant_id: UUID,
    tool_name: str,
    tool_args: dict,
    agent_name: str | None = None,
) -> ApprovalRequest:
    req = ApprovalRequest(
        task_id=task_id,
        tenant_id=tenant_id,
        tool_name=tool_name,
        agent_name=agent_name,
        tool_args=tool_args or {},
        status=ApprovalStatus.pending,
        created_at=_utcnow(),
    )
    session.add(req)
    await session.flush()
    return req


async def decide_request(
    session,
    request_id: UUID,
    decision: str,
    decided_by: UUID | None = None,
    reason: str | None = None,
) -> ApprovalRequest:
    """批准/拒绝一条审批请求（fail-closed：仅 approve 放行）。"""
    req = await session.get(ApprovalRequest, request_id)
    if req is None:
        raise RuntimeError("审批请求不存在")
    if req.status != ApprovalStatus.pending:
        raise RuntimeError(f"审批请求已处理（{req.status.value}），不可重复决策")
    if decision not in ("approve", "reject"):
        raise ValueError("decision 必须是 approve 或 reject")
    req.status = ApprovalStatus.approved if decision == "approve" else ApprovalStatus.rejected
    req.decided_by = decided_by
    req.decision_reason = reason
    req.decided_at = _utcnow()
    return req


async def wait_for_decision(
    session_factory,
    request_id: UUID,
    timeout_seconds: float = 300.0,
    poll_interval: float = 1.0,
) -> str:
    """轮询审批决策直到终态或超时。

    返回 "approve" / "reject"；超时返回 "reject"（fail-closed）。
    """
    elapsed = 0.0
    while elapsed < timeout_seconds:
        async with session_factory() as session:
            req = await session.get(ApprovalRequest, request_id)
            if req is not None and req.status != ApprovalStatus.pending:
                return "approve" if req.status == ApprovalStatus.approved else "reject"
        await asyncio.sleep(poll_interval)
        elapsed += poll_interval
    # 超时：标记过期并拒绝
    async with session_factory() as session:
        req = await session.get(ApprovalRequest, request_id)
        if req is not None and req.status == ApprovalStatus.pending:
            req.status = ApprovalStatus.expired
            req.decided_at = _utcnow()
            await session.commit()
    return "reject"


def make_approval_callback(session_factory, request_id: UUID, timeout_seconds: float = 300.0):
    """构造兼容 ``PobiAgent.set_approval_callback`` 的异步回调。

    pobi_agent 在需要审批时调用该回调，回调阻塞等待 DB 决策并返回
    "approve" / "reject" / "edit:<new_args>"。
    """

    async def _callback(*args, **kwargs) -> str:
        decision = await wait_for_decision(
            session_factory, request_id, timeout_seconds=timeout_seconds
        )
        return decision

    return _callback
