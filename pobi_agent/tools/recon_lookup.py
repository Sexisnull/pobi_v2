"""recon_lookup 工具：利用阶段精确检索本地 RECON 物化库（设计文档 §3.4）。

agent 在探索/利用阶段调用本工具，按 host / 技术 / 路径前缀 / 类别查询本任务
已物化的侦察资产，命中则可短路重复探索，降低 token 消耗与轮次。

实现策略（最小侵入、编排不变）：
- 不依赖注入的 ReconStore，而是依据 ``ctx.deps.session_id`` 作为 task_id，
  从协程级 ``storage_context.get_task_root()`` 定位 ``{task_root}/{task_id}.db``
  （任务级单一库，侦察/利用产物同库不同表），构造临时只读 ReconStore 查询。
- 库文件不存在时返回空结果（表示该任务尚无历史侦察资产），不抛错。
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional

from pydantic_ai import RunContext

from pobi_agent.logging import logger
from pobi_agent.recon import ReconStore
from pobi_agent.recon.store import ReconStoreError
from pobi_agent.storage_context import get_task_root
from pobi_agent.utils.structures import WebappreconDeps

logger = logging.getLogger(__name__)


def _resolve_store(session_id) -> Optional[ReconStore]:
    """依据 session_id + task_root 定位本地 RECON 库；不存在返回 None。"""
    try:
        task_root = get_task_root()
        if task_root is None:
            logger.debug("recon_lookup: task_root 未注入，跳过得物化库")
            return None
        db_path = task_root / f"{session_id}.db"
        if not db_path.exists():
            return None
        # 复用 for_task 定位逻辑（task_root 已知，直接构造）。
        return ReconStore(db_path)
    except Exception as exc:  # noqa: BLE001 - 工具失败不阻断主流程
        logger.warning("recon_lookup: 定位本地库失败（已忽略）: %s", exc)
        return None


def recon_lookup(
    ctx: RunContext[WebappreconDeps],
    host: Optional[str] = None,
    tech: Optional[str] = None,
    path_prefix: Optional[str] = None,
    category: Optional[str] = None,
    keyword: Optional[str] = None,
    limit: int = 20,
) -> Dict[str, object]:
    """检索本任务已物化的侦察资产，命中可短路重复探索。

    Args:
        ctx: 运行时上下文（含 session_id）。
        host: 按主机精确匹配（recon_endpoints.host）。
        tech: 按技术栈/手法匹配（recon_endpoints.tech_stack 或 recon_techniques）。
        path_prefix: 按端点路径前缀匹配（recon_endpoints.path_normalized）。
        category: 按事实类别匹配（recon_facts.category）。
        keyword: 关键词全文检索（FTS5 MATCH），增强模糊召回。
        limit: 返回上限（默认 20）。

    Returns:
        dict: {"found": bool, "count": int, "results": list[dict], "hint": str}
        无本地库或零命中时 found=False，hint 提示 agent 走常规探索。
    """
    empty: Dict[str, object] = {
        "found": False,
        "count": 0,
        "results": [],
        "hint": "无本地侦察资产，请走常规探索流程",
    }
    store = _resolve_store(ctx.deps.session_id)
    if store is None:
        return empty
    try:
        results: List[Dict[str, object]] = store.lookup(
            task_id=str(ctx.deps.session_id),
            host=host,
            tech=tech,
            path_prefix=path_prefix,
            category=category,
            keyword=keyword,
            limit=limit,
        )
        store.close()
    except ReconStoreError as exc:
        logger.warning("recon_lookup: 查询失败（已忽略）: %s", exc)
        return empty
    if not results:
        return empty
    return {
        "found": True,
        "count": len(results),
        "results": results,
        "hint": "命中本地侦察资产，可复用历史结论以缩短探索",
    }
