"""Pobi v2 FastAPI 应用入口。"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from pobi_v2.core.exceptions import register_exception_handlers
from pobi_v2.db.session import Base, engine
from pobi_v2.engine.agent_adapter import install_event_hooks
from pobi_v2.routers import targets, tasks, stream, persistence, auth, approval, report

# 前端静态资源目录（M6 引入的纯静态 SPA）
WEB_DIR = Path(__file__).resolve().parent.parent / "web"
WEB_STATIC_DIR = WEB_DIR / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动时：安装事件钩子，使 CoreAgent 的事件进入 pobi_v2 事件总线
    install_event_hooks()
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

# 挂载前端静态资源（js / css / 资源）；目录为 web/static，对外暴露为 /static
if WEB_STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(WEB_STATIC_DIR)), name="web-static")

app.include_router(auth.router)
app.include_router(targets.router)
app.include_router(tasks.router)
app.include_router(stream.router)
app.include_router(persistence.router)
app.include_router(approval.router)
app.include_router(report.router)


@app.get("/health", tags=["meta"])
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "pobi-v2"}


@app.get("/", tags=["meta"])
async def root() -> dict[str, str]:
    return {"service": "pobi-v2", "docs": "/docs"}


# ---- M6 前端 SPA ----
@app.get("/app", tags=["web"], include_in_schema=False)
async def web_app() -> FileResponse:
    """前端单页应用入口。"""
    index = WEB_DIR / "index.html"
    if not index.exists():
        return FileResponse(index, status_code=200) if index.exists() else FileResponse(
            Path(__file__).resolve().parent.parent / "README.md"
        )
    return FileResponse(index)


@app.get("/web/{path:path}", tags=["web"], include_in_schema=False)
async def web_static_fallback(path: str) -> FileResponse:
    """SPA 静态资源兜底（js / css / 资源）。"""
    candidate = WEB_DIR / path
    if candidate.exists() and candidate.is_file():
        return FileResponse(candidate)
    # 非资源请求回退到 index.html（前端路由用）
    return FileResponse(WEB_DIR / "index.html")
