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
import json
from datetime import datetime, timezone
from uuid import UUID, uuid4

from pobi_v2.core.config import settings
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
    request_id: UUID | None = None,
) -> ApprovalRequest:
    req = ApprovalRequest(
        id=request_id or uuid4(),
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


def make_approval_callback(
    session_factory,
    request_id: UUID | None = None,
    timeout_seconds: float = 300.0,
    tenant_id: UUID | None = None,
    task_id: UUID | None = None,
    auto_approve: bool | None = None,
):
    """构造兼容 ``PobiAgent.set_approval_callback`` 的异步回调。

    pobi_agent 在需要审批时调用该回调。回调会**真实创建**审批请求
    （每次调用生成唯一 request_id，避免同任务多次高危调用主键冲突），
    随后：
    - 若 ``auto_approve``（优先）或 ``settings.auto_approve`` 为真（授权靶场自动化，
      或任务 mode=yolo），直接批准并返回 "approve"；
    - 否则阻塞等待 DB 决策，超时返回 "reject"（fail-closed）。
    """

    async def _callback(*args, **kwargs) -> str:
        # 任务级或全局级放行策略：yolo 模式 / 授权靶场自动化时免人工审批
        effective_auto_approve = (
            auto_approve if auto_approve is not None else settings.auto_approve
        )

        # 解析被拦截的工具名 / 参数（pobi_agent 以关键字或位置参数传入）
        tool_name = kwargs.get("tool_name") or (args[0] if args else None)
        tool_args = kwargs.get("tool_args") or {}
        if isinstance(tool_args, str):
            try:
                tool_args = json.loads(tool_args)
            except (ValueError, TypeError):
                tool_args = {"raw": tool_args}

        # 每次审批回调必须生成唯一 request_id（避免同一任务多次高危调用时
        # 主键冲突，并防止 wait_for_decision 误读到上一次已决审批导致放行/误拒）。
        # 优先采用 pydantic-ai 传入的唯一工具调用标识，否则回落到 uuid4。
        call_id = kwargs.get("tool_call_id") or uuid4()
        if not isinstance(call_id, UUID):
            try:
                call_id = UUID(str(call_id))
            except (ValueError, TypeError):
                call_id = uuid4()

        # 真实创建审批请求（修复断链 + ID 契约对齐）
        if tenant_id is not None:
            try:
                async with session_factory() as session:
                    await create_approval_request(
                        session,
                        task_id=task_id or call_id,
                        tenant_id=tenant_id,
                        tool_name=str(tool_name or "unknown"),
                        tool_args=tool_args if isinstance(tool_args, dict) else {"raw": tool_args},
                        request_id=call_id,
                    )
                    await session.commit()
            except Exception:
                # 创建失败不应阻断 agent；仍走 fail-closed 等待逻辑
                pass

        if effective_auto_approve:
            try:
                async with session_factory() as session:
                    await decide_request(session, call_id, "approve", reason="授权靶场自动批准")
                    await session.commit()
            except Exception:
                pass
            return "approve"

        return await wait_for_decision(
            session_factory, call_id, timeout_seconds=timeout_seconds
        )

    return _callback
