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

import asyncio
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
from pobi_v2.engine.cancel_state import clear_cancel, is_cancelled
from pobi_v2.engine.deadend_runner import run_deadend_agent
from pobi_v2.engine.event_bus import get_session_usage, reset_session_usage


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# 子超时：单个 agent 驱动调用（run_deadend_agent / ScanWorkflow）的墙钟上限。
# 协作式取消只在 agent 调用返回后的检查点生效；若 agent 陷入 LLM 死循环（报告 C：
# DVWA 登录卡死 33 分钟），调用永不返回，cancel 请求永远到不了检查点。用 wait_for
# 给单次调用一个上限，超时即取消内层协程、释放 Worker 槽位，再据 is_cancelled 判定
# 是「被取消」还是「真卡死」。值远大于正常单次 agent 运行（分钟级），仅作兜底熔断。
_AGENT_SUB_TIMEOUT = float(getattr(settings, "agent_sub_timeout_seconds", 30 * 60))


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


async def _publish_status_change(task_id: UUID, new_status: TaskStatus) -> None:
    """把任务终态变更发给事件总线，让前端 SSE 即时感知（不依赖轮询）。"""
    try:
        from pobi_v2.engine.event_bus import bus

        await bus.publish(
            str(task_id),
            {
                "type": "task_status_changed",
                "session_id": str(task_id),
                "task_id": str(task_id),
                "old_status": "running",
                "new_status": new_status.value,
            },
        )
    except Exception:
        # 推送失败不影响终态落库
        pass


async def run_task(ctx, task_id: str, **kwargs: object) -> dict:
    """ARQ 任务入口：执行一次渗透测试任务。

    arq 调用约定为 ``function(ctx, *args)``，故首个参数为任务上下文（未使用）。
    ``**kwargs`` 用于吸收 arq 可能透传的作业选项（如 job_timeout），避免签名报错。

    健壮性：用 ``try/finally`` + ``except BaseException`` 兜底，确保任何退出路径
    （含 ARQ 在 ``job_timeout`` 撞墙时抛出的 ``CancelledError``、普通异常）都能把
    任务终态写回 PG 并发出 ``task_status_changed`` 事件，避免出现“Worker 已丢弃、
    前端仍显示 running”的幽灵任务。
    """
    tid = UUID(task_id)
    await clear_cancel(tid)  # 重置上次运行的中断标志

    try:
        return await _run_task_body(tid)
    except BaseException as exc:  # noqa: BLE001 — 必须覆盖 CancelledError
        if isinstance(exc, asyncio.CancelledError):
            _status = TaskStatus.failed
            _err = "任务被强制中断（可能超过 job_timeout 或 Worker 重启）"
        elif isinstance(exc, Exception):
            _status = TaskStatus.cancelled if (await is_cancelled(tid)) else TaskStatus.failed
            _err = str(exc)
        else:
            _status = TaskStatus.failed
            _err = f"未知退出：{exc!r}"

        # 兜底落库（独立 session，不依赖主体）
        try:
            async with AsyncSessionLocal() as s2:
                t = await s2.get(Task, tid)
                if t is not None and t.status not in (
                    TaskStatus.completed, TaskStatus.cancelled
                ):
                    t.status = _status
                    t.error = _err
                    t.finished_at = _utcnow()
                    # 兜底落库已消耗 token（反映运行中断前的真实用量）
                    _fu = await get_session_usage(str(tid))
                    t.prompt_tokens = _fu["prompt_tokens"]
                    t.completion_tokens = _fu["completion_tokens"]
                    t.total_tokens = _fu["total_tokens"]
                    await record_audit(
                        s2, action="task.terminated",
                        outcome="error" if _status == TaskStatus.failed else "success",
                        detail=_err, task_id=tid, target_id=t.target_id,
                        tenant_id=t.tenant_id,
                    )
                    await s2.commit()
        except Exception:
            pass

        await _publish_status_change(tid, _status)
        # CancelledError 继续向上传播；其余异常已处理，返回结果避免 ARQ 误判重试
        if isinstance(exc, asyncio.CancelledError):
            raise
        return {"task_id": str(tid), "status": _status.value, "error": _err}


async def _run_probe_branch(tid, task, target, hooks, session) -> dict:
    """链路连通性探针分支：轻量、快、全程走共享 Kali，不经过 avfs / 多智能体。

    用 ``asyncio.wait_for`` 套硬超时（probe_runner.PROBE_HARD_TIMEOUT），
    保证即使目标无响应也不会挂死 Worker（此前因重型 DeadEndAgent + avfs 卡死）。
    """
    from pobi_v2.engine import probe_runner

    await record_audit(
        session, action="task.probe_start", outcome="info",
        detail="链路探针：在共享 Kali 中 curl 授权目标并由 LLM 解读",
        task_id=tid, target_id=target.id, tenant_id=task.tenant_id,
    )
    if hooks is not None:
        try:
            hooks.emit_phase_changed(tid, "probe", detail="在共享 Kali 中探测目标连通性")
        except Exception:
            pass

    outcome = await asyncio.wait_for(
        probe_runner.run_probe_agent(
            task=task,
            target=target,
            task_id=tid,
            auto_approve=True,
        ),
        timeout=probe_runner.PROBE_HARD_TIMEOUT,
    )

    reachable = bool((outcome.get("structured_report") or {}).get("target_reachable"))
    await record_audit(
        session, action="task.probe_done", outcome="success" if reachable else "info",
        detail=f"目标连通性：{'可达' if reachable else '不可达'}",
        task_id=tid, target_id=target.id, tenant_id=task.tenant_id,
    )
    return outcome


async def _run_task_body(tid: UUID) -> dict:
    """执行任务主体；异常统一由 ``run_task`` 兜底落库。"""
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
                tenant_id=task.tenant_id,
                meta={"objective": task.objective},
            )
            await session.commit()
            return {"task_id": task_id, "status": "failed", "error": str(exc)}

        task.status = TaskStatus.running
        task.started_at = _utcnow()
        # 清空会话级 token 累计（避免跨任务 / 续跑污染）
        await reset_session_usage(str(tid))
        await session.commit()

        # 取全局事件钩子（已在应用启动时 install_event_hooks 安装，按 session_id==task_id 分发）
        hooks = get_event_hooks()
        # 构建审批回调（fail-closed 的高危工具 gate；yolo 模式免人工审批）
        approval_cb = make_approval_callback(
            AsyncSessionLocal,
            tenant_id=task.tenant_id,
            task_id=tid,
            auto_approve=(task.agent_mode == "yolo"),
        )

        # 分支：链路连通性探针走轻量快路径，不加载重型多智能体 / avfs / RAG。
        if task.kind == "probe":
            outcome = await _run_probe_branch(tid, task, target, hooks, session)
        else:
            # 主路径：直接驱动原 pobi_agent.DeadEndAgent（完整 AI 自主渗透系统，
            # 含 Docker 沙箱执行验证、多智能体协作、ADaPT 规划、ValidationGate、
            # ReporterAgent）。沙箱为必需依赖；若不可用时回退到轻量 ScanWorkflow。
            outcome = None
            engine_kind = "deadend"
            try:
                outcome = await asyncio.wait_for(
                    run_deadend_agent(
                        task=task,
                        target=target,
                        task_id=tid,
                        max_turns=task.max_turns or 50,
                        auto_approve=(task.agent_mode == "yolo"),
                    ),
                    timeout=_AGENT_SUB_TIMEOUT,
                )
            except asyncio.TimeoutError:
                # 单次 agent 调用超过子超时：判定是被取消还是真卡死。
                # cancel 标志优先生效（协作式取消在调用返回前已被请求）。
                if await is_cancelled(tid):
                    task.status = TaskStatus.cancelled
                    task.finished_at = _utcnow()
                    task.error = "任务在子超时内被取消"
                    await record_audit(
                        session, action="task.cancelled", outcome="success",
                        task_id=tid, target_id=target.id,
                    tenant_id=task.tenant_id,
                    )
                    await session.commit()
                    await _publish_status_change(tid, TaskStatus.cancelled)
                    return {"task_id": task_id, "status": "cancelled"}
                # 无取消请求却超时：视为真卡死，交给 run_task 兜底标记 failed。
                raise
            except RuntimeError as exc:
                if "沙箱" in str(exc) or "Docker" in str(exc):
                    # 无 Docker 沙箱：回退到不依赖沙箱的轻量实现，保证可运行
                    from pobi_v2.engine.scan_workflow import ScanWorkflow

                    await record_audit(
                        session, action="task.engine_fallback", outcome="info",
                        detail="Docker 沙箱不可用，回退到 ScanWorkflow（不含沙箱验证）",
                        task_id=tid, target_id=target.id,
                    tenant_id=task.tenant_id,
                    )
                    try:
                        workflow = ScanWorkflow(
                            target=target,
                            task=task,
                            hooks=hooks,
                            approval_callback=approval_cb,
                            model=task.model or settings.model,
                            max_turns=task.max_turns or 50,
                            allow_shell=getattr(settings, "allow_shell_exec", False),
                        )
                        outcome = await asyncio.wait_for(
                            workflow.run(), timeout=_AGENT_SUB_TIMEOUT
                        )
                    except asyncio.TimeoutError:
                        if await is_cancelled(tid):
                            task.status = TaskStatus.cancelled
                            task.finished_at = _utcnow()
                            task.error = "任务在子超时内被取消"
                            await record_audit(
                                session, action="task.cancelled", outcome="success",
                                task_id=tid, target_id=target.id,
                            tenant_id=task.tenant_id,
                            )
                            await session.commit()
                            await _publish_status_change(tid, TaskStatus.cancelled)
                            return {"task_id": task_id, "status": "cancelled"}
                        raise
                    engine_kind = "scan_workflow"
                else:
                    raise

        # 取消检查：若运行期间被请求取消（用异步接口，避免 redis 后端下
        # is_cancelled_sync 在事件循环协程内 run_coroutine_threadsafe 死锁超时）
        if await is_cancelled(tid):
            task.status = TaskStatus.cancelled
            task.finished_at = _utcnow()
            await record_audit(
                session, action="task.cancelled", outcome="success",
                task_id=tid, target_id=target.id,
            tenant_id=task.tenant_id,
            )
            await session.commit()
            await _publish_status_change(tid, TaskStatus.cancelled)
            return {"task_id": task_id, "status": "cancelled"}

        # 落库：运行结果 + 发现 + 轨迹
        task.status = TaskStatus.completed
        task.result = serialize_result(outcome.get("summary")) or ""
        task.confidence = outcome.get("confidence")
        task.finished_at = _utcnow()
        # 落库 token 用量（发送=prompt / 接收=completion）
        usage = await get_session_usage(str(tid))
        task.prompt_tokens = usage["prompt_tokens"]
        task.completion_tokens = usage["completion_tokens"]
        task.total_tokens = usage["total_tokens"]
        await _persist_outcome(session, task, target, outcome)
        await record_audit(
            session, action="task.completed", outcome="success",
            task_id=tid, target_id=target.id,
            tenant_id=task.tenant_id,
            meta={"confidence": outcome.get("confidence")},
        )
        await session.commit()
        await _publish_status_change(tid, TaskStatus.completed)
        return {"task_id": task_id, "status": "completed"}

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
