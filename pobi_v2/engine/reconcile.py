"""任务状态对账核心逻辑（ARQ 幽灵任务 / 取消残留收敛）。

原实现位于 ``routers/system.py`` 的 ``task_reconcile`` 端点，同时被 ARQ Worker
的周期 cron（``worker._auto_reconcile``）复用。该复用造成 **engine → routers 的
反向依赖（分层倒置）**。本模块把对账核心下沉到 engine 层：router 端点与 Worker
cron 统一调用 ``task_reconcile``，消除倒置。

同时承载 ARQ ``job_timeout`` 常量与 worker 健康检查键名，供 worker 与 system
路由共同引用（避免重复定义与 magic number）。
"""
from __future__ import annotations

from datetime import datetime, timezone

from pobi_v2.db.models import Task, TaskStatus
from pobi_v2.db.persistence import record_audit
from pobi_v2.db.session import AsyncSessionLocal
from pobi_v2.engine.cancel_state import is_cancelled
from pobi_v2.engine.queue import get_redis

# 任务执行超时（秒），供对账逻辑引用，避免魔法数字重复
JOB_TIMEOUT = 60 * 60 * 6  # 6h，渗透任务可能较长

# health_check 键：ARQ 默认队列名为 arq:queue，后缀为 :health-check（与 arq.worker 保持一致）。
# 本项目未自定义 queue_name，故直接拼接默认值。
HEALTH_CHECK_KEY = "arq:queue:health-check"

# 处于“活跃”语义、需要被对账的任务状态
_ACTIVE_STATUSES = {TaskStatus.pending, TaskStatus.queued, TaskStatus.running}


async def _job_in_queue(redis, task_id: str) -> bool:
    """判断某 task 对应的 ARQ Job 是否仍在队列或正在被 Worker 持有。

    直接查 arq:queue（zset，待执行）与 arq:in_progress（zset，执行中）两个键。
    enqueue 时已通过 ``_job_id=task_id`` 将 ARQ job_id 与 PG task.id 对齐，
    故此处用 task_id 即可精确匹配。若两个键都不含该 task_id，说明任务已不在
    Worker 调度中（被丢弃/完成/取消）。

    ARQ 0.28 行为补充：任务完成后其 job_id 会从 in_progress 移除并写入结果
    （keep_result 期间）。因此某 task_id 在 arq:queue / arq:in_progress 均不存在
    时，仅代表“当前不在调度中”，需结合 PG 终态判断是否被中途丢弃。
    """
    try:
        queued = await redis.zscore("arq:queue", task_id)
        if queued is not None:
            return True
        in_progress = await redis.zscore("arq:in_progress", task_id)
        if in_progress is not None:
            return True
    except Exception:
        # 探测失败不阻断对账，保守返回 True（视为仍在队列，不误杀）
        return True
    return False


async def _worker_online(redis) -> bool:
    """Worker 是否近期刷新过 health-check 键（在线存活）。"""
    try:
        ttl = await redis.ttl(HEALTH_CHECK_KEY)
        return ttl is not None and ttl > 0
    except Exception:
        return True  # 探测失败保守视为在线，不误杀


async def task_reconcile() -> dict:
    """任务状态对账：确保 PG 中的活跃任务与 ARQ 队列真实状态一致。

    逐条检查活跃任务（pending/queued/running）：
    - 若 cancel_state 已置位 → 标记 cancelled；
    - 若已不在 ARQ 队列/执行中（被丢弃或超时丢弃）→ 标记 failed；
    - 若 started_at 超过 job_timeout 仍未结束 → 标记 failed（超时幽灵任务）。
    所有终止都会把终态写回 PG 并发 task_status_changed 事件供前端即时感知。
    """
    terminated: list[dict] = []
    try:
        from sqlalchemy import select

        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(Task).where(Task.status.in_(_ACTIVE_STATUSES))
            )
            active = list(result.scalars().all())

        redis = await get_redis()
        try:
            worker_alive = await _worker_online(redis)
            for task in active:
                tid = str(task.id)
                new_status: TaskStatus | None = None
                reason: str | None = None

                # 1) 取消标志优先
                if await is_cancelled(task.id):
                    new_status = TaskStatus.cancelled
                    reason = "检测到取消请求，自动终止"
                # 2) 队列中已不存在（被 ARQ 丢弃 / 超时重试上限）。
                #    仅当 Worker 在线时才据此判 failed：若 Worker 已离线，enqueue
                #    信息不可信，交由 job_timeout 逻辑裁决，避免误杀离线期间的任务。
                elif not await _job_in_queue(redis, tid):
                    if worker_alive:
                        new_status = TaskStatus.failed
                        reason = "任务不在 ARQ 队列或执行中，判定为已终止/丢弃"
                    # Worker 离线：不立即判 failed，等待后续在线时或超时逻辑处理
                # 3) 运行超时（started_at 距现在超过 job_timeout）。
                #    Worker 离线或任务不在调度中均触发，防御“Worker 崩溃遗留幽灵”。
                elif task.status == TaskStatus.running and task.started_at is not None:
                    elapsed = (datetime.now(timezone.utc) - task.started_at).total_seconds()
                    if elapsed > JOB_TIMEOUT:
                        new_status = TaskStatus.failed
                        reason = f"运行超过 job_timeout({JOB_TIMEOUT}s)，判定超时终止"

                if new_status is not None:
                    async with AsyncSessionLocal() as s2:
                        t = await s2.get(Task, task.id)
                        if t is not None and t.status in _ACTIVE_STATUSES:
                            t.status = new_status
                            t.error = reason
                            t.finished_at = datetime.now(timezone.utc)
                            await record_audit(
                                s2, action="task.reconciled", actor=t.operator,
                                outcome="error" if new_status == TaskStatus.failed else "success",
                                detail=reason, task_id=task.id, target_id=task.target_id,
                                tenant_id=task.tenant_id,
                            )
                            await s2.commit()
                    # 通知前端
                    try:
                        from pobi_v2.engine.event_bus import bus

                        await bus.publish(
                            tid,
                            {
                                "type": "task_status_changed",
                                "session_id": tid,
                                "task_id": tid,
                                "old_status": task.status.value,
                                "new_status": new_status.value,
                            },
                        )
                    except Exception:
                        pass
                    terminated.append({
                        "task_id": tid,
                        "old_status": task.status.value,
                        "new_status": new_status.value,
                        "reason": reason,
                    })
        finally:
            await redis.aclose()
    except Exception as exc:  # noqa: BLE001 — 对账失败不应 500
        return {"ok": False, "error": f"对账失败：{exc}", "terminated": terminated}

    return {
        "ok": True,
        "scanned": len(active) if "active" in dir() else 0,
        "terminated_count": len(terminated),
        "terminated": terminated,
    }
