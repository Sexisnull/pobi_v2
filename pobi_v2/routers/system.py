"""M8 系统状态：暴露任务消费 Worker（ARQ）的在线情况与队列积压。

ARQ 0.28.0 不使用 arq:workers zset，而是通过 health_check 键表明存活：
- arq:queue:health-check（默认 health_check_key）：Worker 每次循环用 psetex 写入一条
  含统计信息的字符串，TTL = health_check_interval + 1 秒（默认约 31s）。
  只要该键存在（TTL > 0），即说明 Worker 在近期刷新过 → 在线。
- arq:queue：zset，待执行任务队列，zcard 即队列深度。

Redis 不可用时优雅降级，返回 available=False，不抛 500。
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from pobi_v2.core.deps import get_current_user
from pobi_v2.db.models import Task, TaskStatus, User
from pobi_v2.db.persistence import record_audit
from pobi_v2.db.session import AsyncSessionLocal
from pobi_v2.engine.cancel_state import is_cancelled
from pobi_v2.engine.queue import get_redis

router = APIRouter(prefix="/api/v1/system", tags=["system"])

# health_check 键：ARQ 默认队列名为 arq:queue，后缀为 :health-check（与 arq.worker 保持一致）。
# 本项目未自定义 queue_name，故直接拼接默认值。
HEALTH_CHECK_KEY = "arq:queue:health-check"

# 处于“活跃”语义、需要被对账的任务状态
_ACTIVE_STATUSES = {TaskStatus.pending, TaskStatus.queued, TaskStatus.running}


def _parse_health(info: str) -> dict:
    """解析 health-check 字符串，例如：
    'Aug-12 14:10:25 j_complete=0 j_failed=0 j_retried=0 j_ongoing=1 queued=1'
    """
    stats: dict = {}
    for token in info.split():
        if "=" in token:
            k, v = token.split("=", 1)
            try:
                stats[k] = int(v)
            except ValueError:
                stats[k] = v
    return stats


@router.get("/worker-status", dependencies=[Depends(get_current_user)])
async def worker_status() -> dict:
    """返回 ARQ Worker 的在线状态与队列积压。"""
    try:
        redis = await get_redis()
    except Exception as exc:  # noqa: BLE001 — Redis 未就绪时优雅降级
        return {"available": False, "error": f"Redis 不可用：{exc}"}

    try:
        # 在线判定：health-check 键存在且 TTL > 0（Worker 在近期刷新过）
        ttl = await redis.ttl(HEALTH_CHECK_KEY)
        online = ttl is not None and ttl > 0

        info = ""
        stats: dict = {}
        if online:
            raw = await redis.get(HEALTH_CHECK_KEY)
            info = raw.decode() if isinstance(raw, bytes) else str(raw)
            stats = _parse_health(info)

        # 队列积压：arq:queue 中未执行的任务数
        queued = await redis.zcard("arq:queue")

        return {
            "available": True,
            "online": online,
            "online_count": 1 if online else 0,
            "total_workers": 1 if online else 0,
            "queue_depth": int(queued or 0),
            "health_ttl": int(ttl) if ttl and ttl > 0 else 0,
            "stats": stats,
            "detail": info,
        }
    except Exception as exc:  # noqa: BLE001 — 探测失败不应影响主服务
        return {"available": False, "error": f"Worker 状态探测失败：{exc}"}
    finally:
        await redis.aclose()


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


@router.post("/task-reconcile", dependencies=[Depends(get_current_user)])
async def task_reconcile() -> dict:
    """任务状态对账：确保 PG 中的活跃任务与 ARQ 队列真实状态一致。

    逐条检查活跃任务（pending/queued/running）：
    - 若 cancel_state 已置位 → 标记 cancelled；
    - 若已不在 ARQ 队列/执行中（被丢弃或超时丢弃）→ 标记 failed；
    - 若 started_at 超过 job_timeout 仍未结束 → 标记 failed（超时幽灵任务）。
    所有终止都会把终态写回 PG 并发 task_status_changed 事件供前端即时感知。
    """
    from pobi_v2.engine.worker import JOB_TIMEOUT  # ARQ job_timeout（秒）

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
                                s2, action="task.reconciled",
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
