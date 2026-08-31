"""M8 系统状态：暴露任务消费 Worker（ARQ）的在线情况与队列积压。

ARQ 0.28.0 不使用 arq:workers zset，而是通过 health_check 键表明存活：
- arq:queue:health-check（默认 health_check_key）：Worker 每次循环用 psetex 写入一条
  含统计信息的字符串，TTL = health_check_interval + 1 秒（默认约 31s）。
  只要该键存在（TTL > 0），即说明 Worker 在近期刷新过 → 在线。
- arq:queue：zset，待执行任务队列，zcard 即队列深度。

Redis 不可用时优雅降级，返回 available=False，不抛 500。

任务状态对账（``POST /task-reconcile``）的核心逻辑已下沉至
``pobi_v2/engine/reconcile.py``（router 与 Worker cron 统一调用，消除
engine → routers 反向依赖），本模块仅保留端点薄包装。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pobi_v2.core.deps import get_current_user, require_scope
from pobi_v2.db.models import TaskStatus, User
from pobi_v2.engine.queue import get_redis
from pobi_v2.engine.reconcile import HEALTH_CHECK_KEY

router = APIRouter(prefix="/api/v1/system", tags=["system"])


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


@router.post("/task-reconcile", dependencies=[Depends(require_scope("system:write"))])
async def task_reconcile_endpoint() -> dict:
    """任务状态对账：确保 PG 中的活跃任务与 ARQ 队列真实状态一致。

    核心逻辑在 ``pobi_v2.engine.reconcile.task_reconcile``（router 与 Worker
    周期 cron 统一调用），此处仅薄包装端点。
    """
    from pobi_v2.engine.reconcile import task_reconcile

    return await task_reconcile()


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
