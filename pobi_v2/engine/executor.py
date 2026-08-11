"""任务执行管线：被 ARQ Worker 调用，承载扫描工作流运行。

M8 完全复刻：直接驱动原 `pobi_agent.pobi_agent.DeadEndAgent`（完整 AI 自主渗透
系统），包含多智能体协作、Docker 沙箱执行验证、多 LLM、ADaPT 递归规划、
ValidationGate(F lag+Judge) 目标达成验证、ReporterAgent 报告生成。

- 通过 `pobi_v2.engine.deadend_runner.run_deadend_agent` 复用原代码（不重写）。
- 运行轨迹（task_events）、发现（findings）、审计（audit_events）落库。
- 支持取消（cancel_requested）与续跑（attempts 计数）。
- 任务状态机：queued -> running -> completed/failed/cancelled。
- 事件通过全局 `PobiV2EventHooks` 发出（session_id == task_id），前端 SSE 按
  task_id 订阅即可实时收到 thought/tool/confidence/validation/result。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from uuid import UUID

from pobi_agent.hooks import get_event_hooks

from pobi_v2.core.config import settings
from pobi_v2.db.models import Task, TaskStatus, Target
from pobi_v2.db.persistence import (
    record_audit,
    record_finding,
    record_task_event,
    serialize_result,
)
from pobi_v2.db.session import AsyncSessionLocal
from pobi_v2.engine.approval import make_approval_callback
from pobi_v2.engine.cancel_state import clear_cancel, is_cancelled_sync
from pobi_v2.engine.deadend_runner import run_deadend_agent


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def _load_task(session: AsyncSessionLocal, task_id: UUID) -> tuple[Task, Target]:
    task = await session.get(Task, task_id)
    if task is None:
        raise RuntimeError(f"Task {task_id} 不存在")
    target = await session.get(Target, task.target_id)
    if target is None:
        raise RuntimeError(f"Target {task.target_id} 不存在")
    return task, target


def _build_finding_from_report(report: dict) -> list[dict]:
    """从结构化报告解析 findings（复刻原 reporter 的 findings 形态）。"""
    findings = []
    raw = report.get("findings") if isinstance(report, dict) else None
    if isinstance(raw, list):
        for f in raw:
            if isinstance(f, dict):
                findings.append({
                    "title": str(f.get("title", "未命名发现")),
                    "description": str(f.get("description", "")),
                    "severity": str(f.get("severity", "info")).lower(),
                    "confidence": float(f.get("confidence", 0.0) or 0.0),
                    "evidence": str(f.get("evidence", ""))[:4000],
                    "cwe": f.get("cwe"),
                })
    elif isinstance(report, dict) and report.get("summary"):
        # 无结构化 findings 时的兜底：整体作为一条 info 级发现
        findings.append({
            "title": "扫描摘要",
            "description": str(report.get("summary", ""))[:2000],
            "severity": "info",
            "confidence": 0.0,
            "evidence": str(report.get("evidence", ""))[:4000],
            "cwe": None,
        })
    return findings


async def run_task(task_id: str) -> dict:
    """ARQ 任务入口：执行一次渗透测试任务。"""
    tid = UUID(task_id)
    await clear_cancel(tid)  # 重置上次运行的中断标志

    async with AsyncSessionLocal() as session:
        task, target = await _load_task(session, tid)
        task.attempts += 1
        await session.commit()

        # 护栏：校验目标 URL 在授权范围内
        try:
            from pobi_v2.engine.guardrails import assert_in_scope

            assert_in_scope(target, target.url)
        except Exception as exc:
            task.status = TaskStatus.failed
            task.error = f"授权范围校验失败: {exc}"
            task.finished_at = _utcnow()
            await record_audit(
                session, action="task.scope_check", outcome="denied",
                detail=str(exc), task_id=tid, target_id=target.id,
                meta={"objective": task.objective},
            )
            await session.commit()
            return {"task_id": task_id, "status": "failed", "error": str(exc)}

        task.status = TaskStatus.running
        task.started_at = _utcnow()
        await session.commit()

        # 取全局事件钩子（已在应用启动时 install_event_hooks 安装，按 session_id==task_id 分发）
        hooks = get_event_hooks()
        # 构建审批回调（fail-closed 的高危工具 gate）
        approval_cb = make_approval_callback(AsyncSessionLocal, request_id=tid)

        # 主路径：直接驱动原 pobi_agent.DeadEndAgent（完整 AI 自主渗透系统，
        # 含 Docker 沙箱执行验证、多智能体协作、ADaPT 规划、ValidationGate、
        # ReporterAgent）。沙箱为必需依赖；若不可用时回退到轻量 ScanWorkflow。
        outcome = None
        engine_kind = "deadend"
        try:
            outcome = await run_deadend_agent(
                task=task,
                target=target,
                task_id=tid,
                max_turns=task.max_turns or 50,
            )
        except RuntimeError as exc:
            if "沙箱" in str(exc) or "Docker" in str(exc):
                # 无 Docker 沙箱：回退到不依赖沙箱的轻量实现，保证可运行
                from pobi_v2.engine.scan_workflow import ScanWorkflow

                await record_audit(
                    session, action="task.engine_fallback", outcome="info",
                    detail="Docker 沙箱不可用，回退到 ScanWorkflow（不含沙箱验证）",
                    task_id=tid, target_id=target.id,
                )
                workflow = ScanWorkflow(
                    target=target,
                    task=task,
                    hooks=hooks,
                    approval_callback=approval_cb,
                    model=task.model or settings.model,
                    max_turns=task.max_turns or 50,
                    allow_shell=getattr(settings, "allow_shell_exec", False),
                )
                outcome = await workflow.run()
                engine_kind = "scan_workflow"
            else:
                raise

        try:

            # 取消检查：若运行期间被请求取消
            if is_cancelled_sync(tid):
                task.status = TaskStatus.cancelled
                task.finished_at = _utcnow()
                await record_audit(
                    session, action="task.cancelled", outcome="success",
                    task_id=tid, target_id=target.id,
                )
                await session.commit()
                return {"task_id": task_id, "status": "cancelled"}

            # 落库：运行结果 + 发现 + 轨迹
            task.status = TaskStatus.completed
            task.result = serialize_result(outcome.get("summary")) or ""
            task.confidence = outcome.get("confidence")
            task.finished_at = _utcnow()
            await _persist_outcome(session, task, target, outcome)
            await record_audit(
                session, action="task.completed", outcome="success",
                task_id=tid, target_id=target.id,
                meta={"confidence": outcome.get("confidence")},
            )
            await session.commit()
            return {"task_id": task_id, "status": "completed"}
        except Exception as exc:
            if is_cancelled_sync(tid):
                task.status = TaskStatus.cancelled
            else:
                task.status = TaskStatus.failed
                task.error = str(exc)
            task.finished_at = _utcnow()
            await record_audit(
                session, action="task.failed", outcome="error",
                detail=str(exc), task_id=tid, target_id=target.id,
            )
            await session.commit()
            return {"task_id": task_id, "status": task.status.value, "error": str(exc)}


async def _persist_outcome(session, task: Task, target: Target, outcome: dict) -> None:
    """把工作流产出落库：运行轨迹事件 + 结构化发现。"""
    # 1) 运行轨迹（复刻原 context_engine 的 session 记忆落库）
    await record_task_event(
        session, task.id, "agent_result",
        {"summary": (outcome.get("summary") or "")[:2000]},
    )

    # 2) 结构化发现（复刻原 reporter 的 findings 落库）
    structured = outcome.get("structured_report") or {}
    for f in _build_finding_from_report(structured):
        await record_finding(
            session,
            task_id=task.id,
            target_id=target.id,
            title=f["title"],
            description=f["description"],
            severity=f["severity"],
            confidence=f["confidence"],
            evidence={"text": f["evidence"]},
            cwe=f["cwe"],
        )
