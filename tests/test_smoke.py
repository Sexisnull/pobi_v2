"""M1 冒烟测试：应用加载、路由注册、事件总线（不依赖外部数据库）。

数据库端到端测试需 Postgres，见 README 的 docker-compose + alembic 步骤。
"""
from __future__ import annotations

import asyncio

from pobi_v2.main import app
from pobi_v2.routers import targets, tasks
from pobi_v2.engine.event_bus import PobiV2EventHooks, bus


def test_app_loads():
    paths = {getattr(r, "path", "") for r in app.routes}
    assert "/" in paths and "/health" in paths


def test_routers_registered():
    target_paths = {r.path for r in targets.router.routes}
    task_paths = {r.path for r in tasks.router.routes}
    assert "/api/v1/targets" in target_paths
    assert "/api/v1/tasks" in task_paths
    assert any("target_id" in p for p in target_paths)
    assert any("task_id" in p for p in task_paths)


def test_event_bus_roundtrip():
    async def _run():
        h = PobiV2EventHooks()
        q = await bus.subscribe("s_test")
        h.emit_agent_thought("s_test", "agent", "hello")
        evt = await asyncio.wait_for(q.get(), timeout=2)
        await bus.unsubscribe("s_test", q)
        return evt

    evt = asyncio.run(_run())
    assert evt["type"] == "agent_thought"
    assert evt["thought"] == "hello"
