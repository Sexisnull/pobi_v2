"""任务认证前置（PreAuth）路由。

任务创建阶段完成登录的配套端点：
- 查看认证状态
- 触发自动认证（auth_mode=auto，使用任务已保存的凭据）
- 手动分支：启动一次性浏览器 / 轮询截图 / 发送交互指令 / 捕获会话 / 销毁

认证结果落盘到 ``tasks/<task_id>/agent/auth_context``（原生 AuthContext 三件套），
并写入 recon_facts 供 L0/L1 注入。会话严格按 task_id 隔离。
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from pobi_agent.constants import TASKS_ROOT
from pobi_agent.logging import logger

from pobi_v2.core.deps import require_scope
from pobi_v2.core.exceptions import NotFoundError
from pobi_v2.db.models import Task, User
from pobi_v2.db.session import get_session
from pobi_v2.engine.preauth import (
    # [DISABLED 2026-09-01] 手动登录分支搁置（MFA 人工流程暂缓，见 .ai/roadmap.md）
    # ManualAuthSession,
    # get_manual_session,
    # register_manual_session,
    run_auto_auth,
    # unregister_manual_session,
)

router = APIRouter(prefix="/api/v1/tasks/{task_id}/auth", tags=["task-auth"])


class AutoAuthIn(BaseModel):
    """可选：覆盖任务已保存的凭据触发自动认证。字段可空（缺省用任务凭据）。"""

    username: str | None = Field(default=None, max_length=255)
    password: str | None = Field(default=None, max_length=4096)
    login_url: str | None = Field(default=None, max_length=2048)


# [DISABLED 2026-09-01] 手动登录分支搁置（MFA 人工流程暂缓，见 .ai/roadmap.md）
# class ManualActionIn(BaseModel):
#     """手动分支交互指令。action_type：navigate / click / fill / press / eval / wait。"""
#
#     action_type: str = Field(..., pattern="^(navigate|click|fill|press|eval|wait)$")
#     selector: str | None = None
#     text: str | None = None
#     key: str | None = None
#     url: str | None = None
#     script: str | None = None
#     ms: int | None = Field(default=None, ge=100, le=30_000)


async def _load_task(task_id: UUID, session: AsyncSession, user: User) -> Task:
    task = await session.get(Task, task_id, options=[selectinload(Task.target)])
    if task is None or task.tenant_id != user.tenant_id:
        raise NotFoundError("任务不存在")
    return task


def _task_root(task_id: UUID) -> Path:
    root = TASKS_ROOT / str(task_id)
    root.mkdir(parents=True, exist_ok=True)
    return root


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@router.get("/status")
async def auth_status(
    task_id: UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_scope("tasks:read")),
) -> dict:
    """返回任务认证状态与已落盘会话情况。"""
    task = await _load_task(task_id, session, user)
    storage_path = None
    profile = task.auth_profile or "preauth"
    try:
        storage = Path(_task_root(task_id)) / "agent" / "auth_context" / f"{profile}.playwright.json"
        if storage.exists():
            storage_path = str(storage)
    except Exception:  # noqa: BLE001 — 状态查询不因文件探测异常而失败
        storage_path = None
    # [DISABLED 2026-09-01] 手动登录分支搁置，manual_session_active 恒 False
    # manual = get_manual_session(str(task_id))
    return {
        "task_id": str(task_id),
        "auth_mode": task.auth_mode,
        "auth_status": task.auth_status,
        "auth_profile": profile,
        "auth_username": task.auth_username,
        "auth_login_url": task.auth_login_url,
        "auth_error": task.auth_error,
        "auth_updated_at": task.auth_updated_at.isoformat() if task.auth_updated_at else None,
        "storage_path": storage_path,
        "manual_session_active": False,
    }


@router.post("/auto", status_code=status.HTTP_202_ACCEPTED)
async def trigger_auto_auth(
    task_id: UUID,
    body: AutoAuthIn | None = None,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_scope("tasks:write")),
) -> dict:
    """触发自动认证（复用任务已保存凭据，或 body 覆盖）。返回运行结果摘要。"""
    task = await _load_task(task_id, session, user)
    username = body.username if body and body.username else task.auth_username
    password = body.password if body and body.password else None
    if not password:
        # 密码不落库：从任务目录钱包回退读取（凭据随任务走）
        from pobi_agent.auth_resolver import CredentialsStore
        from pobi_agent.storage_context import clear_task_root, set_task_root

        _tok = set_task_root(_task_root(task_id))
        try:
            password = CredentialsStore.resolve(
                task.target.url, task.auth_profile or "preauth"
            ).password
        finally:
            clear_task_root(_tok)
    login_url = body.login_url if body and body.login_url else task.auth_login_url
    if not username or not password:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="缺少账号密码凭据")
    if not task.target:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="任务关联目标缺失")

    task.auth_status = "running"
    task.auth_updated_at = _utcnow()
    await session.commit()

    result = await run_auto_auth(
        task_id=str(task_id),
        task_root=_task_root(task_id),
        target=task.target.url,
        login_url=login_url,
        username=username,
        password=password,
        profile=task.auth_profile or "preauth",
    )
    task.auth_status = result["status"]
    task.auth_error = result.get("error")
    task.auth_updated_at = _utcnow()
    await session.commit()
    return {"task_id": str(task_id), "auth_status": task.auth_status, **result}


# ============================================================================
# [DISABLED 2026-09-01] 手动登录分支（MFA 人工流程）已整体搁置，见 .ai/roadmap.md MFA 演进计划。
# 以下 5 个端点 /manual/* 暂时注释停用；恢复时去掉本段与各端点的行首注释即可。
# 相关：pobi_agent 无改动；preauth.py 中 ManualAuthSession/_MANUAL_SESSIONS 同步注释。
# ============================================================================
# @router.post("/manual/start", status_code=status.HTTP_202_ACCEPTED)
# async def manual_start(
#     task_id: UUID,
#     session: AsyncSession = Depends(get_session),
#     user: User = Depends(require_scope("tasks:write")),
# ) -> dict:
#     """启动一次性手动登录浏览器。"""
#     task = await _load_task(task_id, session, user)
#     if not task.target:
#         raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="任务关联目标缺失")
#     login_url = task.auth_login_url or task.target.url
#     existing = get_manual_session(str(task_id))
#     if existing and existing.started:
#         return {"task_id": str(task_id), "ok": True, "note": "浏览器已在运行"}
#
#     session_obj = ManualAuthSession(
#         task_id=str(task_id),
#         task_root=_task_root(task_id),
#         target=task.target.url,
#         login_url=login_url,
#         profile=task.auth_profile or "preauth",
#     )
#     register_manual_session(str(task_id), session_obj)
#     try:
#         await session_obj.start()
#         snap = await session_obj.snapshot()
#     except Exception as exc:  # noqa: BLE001 — 启动失败需清理并返回明确错误
#         unregister_manual_session(str(task_id))
#         raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"浏览器启动失败: {exc}") from exc
#     task.auth_status = "running"
#     await session.commit()
#     return {"task_id": str(task_id), "ok": True, **snap}
#
#
# @router.get("/manual/snapshot")
# async def manual_snapshot(
#     task_id: UUID,
#     session: AsyncSession = Depends(get_session),
#     user: User = Depends(require_scope("tasks:read")),
# ) -> dict:
#     """轮询取当前浏览器画面（base64 截图 + url/title）。"""
#     session_obj = get_manual_session(str(task_id))
#     if session_obj is None or not session_obj.started:
#         raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="手动浏览器未在运行")
#     if session_obj.expired():
#         await session_obj.stop()
#         unregister_manual_session(str(task_id))
#         raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="会话超时已销毁，请重新启动")
#     return await session_obj.snapshot()
#
#
# @router.post("/manual/action")
# async def manual_action(
#     task_id: UUID,
#     body: ManualActionIn,
#     session: AsyncSession = Depends(get_session),
#     user: User = Depends(require_scope("tasks:write")),
# ) -> dict:
#     """向手动浏览器发送交互指令。"""
#     session_obj = get_manual_session(str(task_id))
#     if session_obj is None or not session_obj.started:
#         raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="手动浏览器未在运行")
#     params = {
#         "selector": body.selector,
#         "text": body.text,
#         "key": body.key,
#         "url": body.url,
#         "script": body.script,
#         "ms": body.ms,
#     }
#     return await session_obj.action(body.action_type, **params)
#
#
# @router.post("/manual/capture")
# async def manual_capture(
#     task_id: UUID,
#     session: AsyncSession = Depends(get_session),
#     user: User = Depends(require_scope("tasks:write")),
# ) -> dict:
#     """完成捕获：导出浏览器状态并落盘为 AuthContext 三件套，随后销毁浏览器。"""
#     task = await _load_task(task_id, session, user)
#     session_obj = get_manual_session(str(task_id))
#     if session_obj is None or not session_obj.started:
#         raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="手动浏览器未在运行")
#     try:
#         result = await session_obj.capture()
#         if not result.get("ok"):
#             raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=result.get("error", "捕获失败"))
#     finally:
#         await session_obj.stop()
#         unregister_manual_session(str(task_id))
#     task.auth_status = "success"
#     task.auth_error = None
#     task.auth_updated_at = _utcnow()
#     await session.commit()
#     return {"task_id": str(task_id), **result}
#
#
# @router.post("/manual/abort")
# async def manual_abort(
#     task_id: UUID,
#     session: AsyncSession = Depends(get_session),
#     user: User = Depends(require_scope("tasks:write")),
# ) -> dict:
#     """销毁手动浏览器会话。"""
#     session_obj = get_manual_session(str(task_id))
#     if session_obj is not None:
#         await session_obj.stop()
#         unregister_manual_session(str(task_id))
#     return {"task_id": str(task_id), "ok": True}
