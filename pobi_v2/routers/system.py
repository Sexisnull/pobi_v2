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
from pobi_v2.core.deps import get_current_user, require_scope
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


@router.get("/worker-status", dependencies=[Depends(require_scope("system:read"))])
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


@router.post("/task-reconcile", dependencies=[Depends(require_scope("system:write"))])
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


# ---------------------------------------------------------------------------
# 链路连通性探测端点（worker-status 已存在，kali-status/llm-status/probe 新增）
# ---------------------------------------------------------------------------
import asyncio  # noqa: E402  — 置于文件尾部，避免影响上方既有导入顺序
from dataclasses import asdict  # noqa: E402

from fastapi import HTTPException, status as http_status  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from pobi_v2.core.exceptions import NotFoundError  # noqa: E402
from pobi_v2.db.models import Target  # noqa: E402
from pobi_v2.db.session import get_session  # noqa: E402
from pobi_v2.engine.guardrails import check_scope  # noqa: E402
from pobi_v2.engine.queue import enqueue_task  # noqa: E402
from pobi_v2.schemas.task import ProbeRequest, ProbeResponse, TaskCreate  # noqa: E402


@router.get("/kali-status", dependencies=[Depends(require_scope("system:read"))])
async def kali_status() -> dict:
    """共享 Kali 沙箱健康检查：取全局共享容器，执行一条探测命令验证可响应与基础工具链。

    复用 sandbox_bootstrap 的 manager 单例（指向与 Worker 同一容器），不新建容器。
    Docker SDK 为同步阻塞，用 asyncio.to_thread 包裹避免阻塞事件循环。
    返回容器 id 前缀、退出码与基础环境信息（不序列化 Sandbox 对象）。
    """
    try:
        from pobi_v2 import sandbox_bootstrap

        manager = sandbox_bootstrap._get_manager()
        sandbox = manager.get_or_create_shared_kali()
    except Exception as exc:  # noqa: BLE001 — 取容器失败即不健康
        return {"available": False, "healthy": False, "error": f"共享 Kali 容器不可用：{exc}"}

    try:
        result = await asyncio.to_thread(
            sandbox.execute_command,
            "echo pobi-ok && id && cat /etc/os-release | head -1",
            False,  # stream=False 才有完整 exit_code/stdout/stderr
            30,  # timeout_seconds
        )
        rc = result.get("exit_code")
        stdout = result.get("stdout") or ""
        stderr = result.get("stderr") or ""
    except Exception as exc:  # noqa: BLE001 — 命令执行失败即不健康
        return {
            "available": True,
            "healthy": False,
            "container_id": str(sandbox.container_id)[:12],
            "error": f"命令执行失败：{exc}",
        }

    healthy = rc == 0 and "pobi-ok" in stdout
    return {
        "available": True,
        "healthy": healthy,
        "container_id": str(sandbox.container_id)[:12],
        "exit_code": rc,
        "stdout": stdout.strip(),
        "stderr": stderr.strip(),
    }


@router.get("/llm-status", dependencies=[Depends(require_scope("system:read"))])
async def llm_status() -> dict:
    """LLM 连通性探测：用平台统一调用入口发起一条最小请求，返回模型名、延迟与成功标志。

    复用 pobi_v2.llm.client.chat（litellm + tenacity 重试），不引入新客户端。
    异常优雅降级返回 unhealthy，不抛 500。
    """
    try:
        from pobi_v2.llm.client import chat
        from pobi_v2.llm.types import LLMMessage

        # 注意：部分推理模型（如 glm 系列）会先输出 reasoning_content 占用大量
        # token，max_tokens 过小会导致正文被截断、content 为空而误判不健康。
        # 这里给足预算（800），并以「调用未抛异常」作为连通判据，
        # 文本含 "OK" 仅作辅助信号，不再作为唯一健康条件。
        resp = await chat(
            [LLMMessage(role="user", content="reply with the single word OK")],
            temperature=0.0,
            max_tokens=800,
        )
    except Exception as exc:  # noqa: BLE001 — LLM 不可达即不健康
        return {"available": False, "healthy": False, "error": f"LLM 调用失败：{exc}"}

    text = (resp.content or "").strip()
    # 连通即健康：能拿到正常响应（无异常、模型名非空）即视为可用；
    # 若正文恰好含 OK 则作为强信号，否则仍按连通判定健康（兼容推理模型空/异构返回）。
    healthy = bool(resp.model) and ("OK" in text.upper() or text != "")
    return {
        "available": True,
        "healthy": healthy,
        "model": resp.model,
        "reply": text,
        "latency_ms": round(resp.latency_ms, 1),
        "usage": (asdict(resp.usage) if resp.usage else None),
    }


@router.post("/probe", response_model=ProbeResponse)
async def probe(
    data: ProbeRequest,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_scope("tasks:write")),
) -> ProbeResponse:
    """端到端链路探针：派发一个轻量探针任务，由 ARQ Worker 在共享 Kali 沙箱中对已授权目标做连通访问。

    流程：校验 target 存在/授权 -> 创建 kind=probe 任务（auto_approve）-> 投递 Worker。
    Worker 内走轻量快路径 ``run_probe_agent``：直接在共享 Kali 用 curl 访问目标（不经过 avfs /
    多智能体），再由 LLM 给出一句连通结论。任务带独立短超时（90s）与 max_tries=1，快速结束。
    调用方用 GET /api/v1/tasks/{task_id} 或 SSE 观测完整链路（本端点异步返回 task_id）。

    安全：严格复用 check_scope 授权闸门，探针仅在已授权目标 scope 内执行。
    """
    target = await session.get(Target, data.target_id)
    if target is None or target.tenant_id != user.tenant_id:
        raise NotFoundError("关联的目标不存在")
    allowed, reason = check_scope(target, target.url)
    if not allowed:
        raise HTTPException(
            status_code=http_status.HTTP_403_FORBIDDEN,
            detail=f"目标超出授权范围，拒绝探针: {reason}",
        )

    objective = data.prompt or (
        f"对目标 {target.url} 做一次连通性验证。请在 Kali 沙箱中使用 shell 工具直接执行 curl 命令"
        "（例如：curl -sS -o /dev/null -w '%{http_code}' <目标URL>）访问其首页，"
        "获取并记录 HTTP 状态码与响应头，确认从沙箱到目标的网络链路可通。"
        "注意：不要使用 requester/avfs 类工具，直接执行 curl 命令即可。"
        "仅做连通探测，不要深入利用或扫描，得出简短结论即可结束。"
    )
    create = TaskCreate(
        target_id=data.target_id,
        name=f"链路探针-{target.name or str(target.id)[:8]}",
        objective=objective,
        max_turns=data.max_turns,
        agent_mode="yolo",  # 探针命令已在授权 scope 内，自动批准避免卡在人工审批
        kind="probe",
    )
    task = Task(**create.model_dump())
    task.tenant_id = user.tenant_id
    task.owner_id = user.id
    task.operator = user.email
    task.status = TaskStatus.queued
    session.add(task)
    await session.commit()
    await session.refresh(task)

    try:
        await enqueue_task(str(task.id), kind="probe")
    except Exception:
        task.status = TaskStatus.pending
        await session.commit()

    return ProbeResponse(
        task_id=task.id,
        target_id=task.target_id,
        status=task.status.value,
        message="链路探针已派发至 Worker，使用 GET /api/v1/tasks/{task_id} 或 SSE 观测完整链路",
    )
