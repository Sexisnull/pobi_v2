"""SSE 实时推送端点。

订阅事件总线中对应 task_id（作为 session_id）的事件，向前端实时推送
Agent 的思考、工具调用、置信度变化、状态流转等。
"""
from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Depends, HTTPException, Request
from sse_starlette.sse import EventSourceResponse

from pobi_v2.core.deps import get_current_user
from pobi_v2.db.session import AsyncSessionLocal
from pobi_v2.db.models import Task, User
from pobi_v2.engine.event_bus import bus

router = APIRouter(tags=["stream"], dependencies=[Depends(get_current_user)])


@router.get("/api/v1/tasks/{task_id}/stream")
async def task_stream(
    task_id: str,
    request: Request,
    user: User = Depends(get_current_user),
):
    async with AsyncSessionLocal() as session:
        task = await session.get(Task, task_id)
        if task is None or task.tenant_id != user.tenant_id:
            raise HTTPException(status_code=404, detail="任务不存在")

    async def event_generator():
        queue = await bus.subscribe(task_id)
        try:
            # 先发送当前任务快照
            yield {
                "event": "snapshot",
                "data": json.dumps(
                    {"task_id": task_id, "status": task.status.value},
                    ensure_ascii=False,
                ),
            }
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=30)
                except asyncio.TimeoutError:
                    # 心跳保活
                    yield {"event": "ping", "data": "{}"}
                    continue
                yield {
                    "event": event.get("type", "event"),
                    "data": json.dumps(event, ensure_ascii=False, default=str),
                }
                # 任务终态后关闭流
                if event.get("type") in ("agent_end", "agent_error") or (
                    event.get("type") == "task_status_changed"
                    and event.get("new_status") in ("completed", "failed", "cancelled")
                ):
                    break
        finally:
            await bus.unsubscribe(task_id, queue)

    return EventSourceResponse(event_generator())
