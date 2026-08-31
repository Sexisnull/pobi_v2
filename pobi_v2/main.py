"""Pobi v2 FastAPI 应用入口。"""
from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from pobi_agent.logging import logger, setup_logging
from pobi_v2.core.exceptions import register_exception_handlers
from pobi_v2.core.seed import seed_admin_if_needed
from pobi_v2.core.config import settings
from pobi_v2.sandbox_bootstrap import ensure_shared_kali_ready
from pobi_v2.db.session import Base, engine
from pobi_v2.engine.agent_adapter import install_event_hooks
from pobi_v2.engine.event_bus import persist_event_worker
from pobi_v2.routers import (
    targets,
    tasks,
    stream,
    persistence,
    auth,
    approval,
    report,
    system,
    instruction,
    pricing,
    api_tokens,
)

# 统一日志格式（去 ANSI 颜色、带中国时区时间戳）；写文件到 $POBI_V2_LOG_DIR/api.log
LOG_DIR = Path(settings.log_dir)
LOG_DIR.mkdir(parents=True, exist_ok=True)
setup_logging(level=logging.INFO, log_file=str(LOG_DIR / "api.log"))

# 前端目录：React 构建产物经 Vite 输出到 web/spa，由 nginx 直接静态托管（/ 即控制台）。
# FastAPI 仅保留 /app 路由作为容器内回退/兼容别名。
WEB_DIR = Path(__file__).resolve().parent.parent / "web"
WEB_SPA_DIR = WEB_DIR / "spa"


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动时：安装事件钩子，使 CoreAgent 的事件进入 pobi_v2 事件总线
    install_event_hooks()
    # 后台持久化 plan_step 事件（供执行计划聚合端点读取）
    asyncio.create_task(persist_event_worker())
    # 注：RECON 本地库 → PG 聚合层同步已改为同进程直连（ContextEngine 内部
    # 经 pg_session_factory 直调 upsert_to_pg + deadend_runner finally 兜底 flush），
    # 不再依赖跨进程的 recon_sync_worker，故此处不再启动。
    # 首次启动时自动创建 admin 账号（幂等：仅当库内无用户时）
    await seed_admin_if_needed()
    # 启动阶段确保全局共享 Kali 沙箱容器就绪（全面容器化核心依赖）。
    # 失败即 fail-fast，避免任务运行时才发现沙箱不可用。
    await ensure_shared_kali_ready()
    yield
    # 关闭时：释放连接池
    await engine.dispose()


app = FastAPI(title="Pobi v2", version="1.0.0", lifespan=lifespan)

register_exception_handlers(app)

# CORS：允许前端独立端口调试（如 vite / http-server）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# React 构建产物（vite base=/）的 /app 兼容别名：挂载 assets 供 /app/ 反代回退使用。
# 生产由 nginx 直接静态托管 web/spa，此挂载仅用于容器内 /app 路径兜底。
if WEB_SPA_DIR.exists():
    app.mount("/app/assets", StaticFiles(directory=str(WEB_SPA_DIR / "assets")), name="web-app-assets")

app.include_router(auth.router)
app.include_router(targets.router)
app.include_router(tasks.router)
app.include_router(instruction.router)
app.include_router(stream.router)
app.include_router(persistence.router)
app.include_router(approval.router)
app.include_router(report.router)
app.include_router(system.router)
app.include_router(pricing.router)
app.include_router(api_tokens.router)


@app.get("/health", tags=["meta"])
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "pobi-v2"}


@app.get("/", tags=["meta"])
async def root() -> dict[str, str]:
    return {"service": "pobi-v2", "docs": "/docs"}


# ---- React 前端 SPA（/app 路径，托管 web/spa）----
@app.get("/app", tags=["web"], include_in_schema=False)
async def web_app() -> FileResponse:
    """React 前端单页应用入口（web/spa/index.html）。"""
    index = WEB_SPA_DIR / "index.html"
    if not index.exists():
        # 前端产物缺失（如未执行 webapp 构建）时兜底返回项目 README，避免 404。
        return FileResponse(Path(__file__).resolve().parent.parent / "README.md")
    return FileResponse(index)


@app.get("/app/{path:path}", tags=["web"], include_in_schema=False)
async def web_app_fallback(path: str) -> FileResponse:
    """React 前端路由兜底：真实文件直接返回，其余回退 index.html 交由前端路由处理。"""
    candidate = (WEB_SPA_DIR / path).resolve()
    if candidate.is_file() and WEB_SPA_DIR.resolve() in candidate.parents:
        return FileResponse(candidate)
    index = WEB_SPA_DIR / "index.html"
    if not index.exists():
        return FileResponse(Path(__file__).resolve().parent.parent / "README.md")
    return FileResponse(index)




