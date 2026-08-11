"""驱动原 pobi_agent 的 DeadEndAgent 运行完整的 AI 自主渗透测试。

本模块是 pobi_v2 与原 pobi 自主渗透引擎之间的**集成适配层**，目标是
「完整复刻、不轻量化、直接复用原代码」：

- 直接实例化并驱动原 ``pobi_agent.pobi_agent.DeadEndAgent``（多智能体协作：
  SupervisorAgent 通过工具委派调用 requester / python_interpreter / shell /
  webapp_analyzer / memory / authenticator 等子 Agent）。
- 复用原 ``pobi_agent`` 的全部基础设施：``ScopePolicy`` 授权闸门、
  ``Sandbox``(Docker) 沙箱执行验证、``ModelSpec`` 多 LLM、``ContextEngine``
  上下文引擎、ADaPT 递归规划、``ValidationGate``(Flag+Judge) 目标达成验证、
  ``ReporterAgent`` 报告生成、``EmbedderClient``/``SqliteRagConnector`` 检索。
- 通过全局已注册的 ``PobiV2EventHooks`` 把安全事件（thought / tool / step /
  confidence / validation / result）实时写入 pobi_v2 的数据库与 SSE 流
  （session_id == task_id）。
- 通过 ``make_approval_callback`` 接入 pobi_v2 的高危工具审批系统
  （fail-closed）。
- **授权范围**：原 pobi 的 scope 闸门在 ``pw_requester`` 网络出口处通过
  ``check_scope`` 读取全局 ``~/.cache/pobi/scope.yaml`` 执行。本适配层在运行
  前把 pobi_v2 的 ``Target`` 范围写入该文件，从而**完整复用原 scope 逻辑**
  （不重写）。

原 pobi 的 ``DeadEndAgent`` 已把全部子 Agent、沙箱、规划、验证、报告封装在
内部（见 ``pobi_agent/agents/components/executor.py`` 与
``pobi_agent/pobi_agent.py``），本层只负责把 pobi_v2 的输入适配进去并把产出
回写。
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any, Optional
from uuid import UUID

import yaml
from pobi_agent.config.settings import Config, ModelSpec
from pobi_agent.core import (
    init_rag_session_manager,
    sandbox_setup,
)
from pobi_agent.hooks import get_event_hooks
from pobi_agent.models.registry import EmbedderClient
from pobi_agent.pobi_agent import DeadEndAgent
from pobi_agent.rag.sqlite_connector import SqliteRagConnector
from pobi_agent.sandbox.sandbox_manager import SandboxManager
from pobi_agent.scope import DEFAULT_PATH as SCOPE_YAML_PATH

from pobi_v2.core.config import settings
from pobi_v2.db.models import Task, Target
from pobi_v2.engine.approval import make_approval_callback


# provider -> (api_key 环境变量, base_url 环境变量)
_PROVIDER_ENV: dict[str, tuple[Optional[str], Optional[str]]] = {
    "anthropic": ("ANTHROPIC_API_KEY", None),
    "openai": ("OPENAI_API_KEY", None),
    "open_router": ("OPEN_ROUTER_API_KEY", None),
    "openrouter": ("OPEN_ROUTER_API_KEY", None),
    "gemini": ("GEMINI_API_KEY", None),
    "google": ("GEMINI_API_KEY", None),
    "requesty": ("REQUESTY_API_KEY", "REQUESTY_BASE_URL"),
    "local": ("LOCAL_API_KEY", "LOCAL_BASE_URL"),
}


# ---------------------------------------------------------------------------
# 多 LLM：从 pobi_v2 配置解析 ``scheme/model``，复用原 pobi_agent 的 ModelSpec
# ---------------------------------------------------------------------------
def _build_model_spec(model_str: Optional[str]) -> ModelSpec:
    """复用原 ``ModelSpec(provider, model_name, api_key, base_url)`` 构造多 LLM。

    ``model_str`` 形如 ``anthropic/claude-sonnet-4`` / ``openai/gpt-4o`` /
    ``openrouter/...`` / ``gemini/...`` / ``local/<alias>``。provider 段映射到
    对应的 API Key / Base URL 环境变量（与原 pobi 的 Config 行为一致）。
    """
    raw = (model_str or settings.model or "").strip()
    if "/" in raw:
        provider, model_name = raw.split("/", 1)
    else:
        provider, model_name = "anthropic", raw or "claude-sonnet-4"

    provider = provider.lower()
    key_env, url_env = _PROVIDER_ENV.get(provider, (None, None))
    api_key = os.environ.get(key_env) if key_env else None
    base_url = os.environ.get(url_env) if url_env else None

    return ModelSpec(
        provider=provider,
        model_name=model_name,
        api_key=api_key,
        base_url=base_url,
    )


# ---------------------------------------------------------------------------
# 授权范围：把 pobi_v2 的 Target 写入全局 scope.yaml，复用原 ScopePolicy 闸门
# ---------------------------------------------------------------------------
def _write_scope_file(target: Target) -> Path:
    """把 pobi_v2 的 Target 范围写入原 pobi 的全局 scope.yaml。

    原 pobi 的授权闸门在 ``pw_requester`` 网络出口处通过 ``check_scope`` 读取
    该文件。写入后，原 scope 逻辑自动对本次任务生效（不重写 scope 代码）。
    """
    import tldextract

    def _root_host(url: str) -> str:
        ext = tldextract.extract(url or "")
        if ext.registered_domain:
            return ext.registered_domain
        return (url or "").split("://", 1)[-1].split("/", 1)[0].split(":", 1)[0]

    root_domains = [_root_host(target.url)] if target.url else []
    for extra in (target.in_scope or []):
        root_domains.append(_root_host(extra))
    root_domains = [d for d in root_domains if d]

    out_of_scope = list(target.out_of_scope or [])

    scope_doc: dict[str, Any] = {
        "enabled": bool(target.enabled and (target.in_scope or target.url)),
        "root_domains": root_domains,
        "domains": [],
        "ips": [],
        "out_of_scope": out_of_scope,
        "max_qps": getattr(settings, "scope_max_qps", 10),
        "max_bytes": getattr(settings, "scope_max_bytes", 5_000_000),
    }

    SCOPE_YAML_PATH.parent.mkdir(parents=True, exist_ok=True)
    SCOPE_YAML_PATH.write_text(
        yaml.safe_dump(scope_doc, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    return SCOPE_YAML_PATH


# ---------------------------------------------------------------------------
# 可用 Agent 能力集合（与 jsonrpc_server._build_available_agents 保持一致）
# ---------------------------------------------------------------------------
def _build_available_agents(
    sandbox_manager: SandboxManager,
) -> dict[str, str]:
    """构造 ``DeadEndAgent`` 的 ``available_agents`` 能力字典。

    与原 pobi Web Console 的 ``_build_available_agents`` 保持一致：
    - 无 Docker 沙箱时移除 ``shell`` / ``python_interpreter``（沙箱验证不可用）。
    - ``webapp_analyzer`` 仅在启用浏览器分析时提供。
    """
    sandbox_ok = False
    try:
        sb = sandbox_manager.create_sandbox()
        sandbox_ok = sb is not None
    except Exception:
        sandbox_ok = False

    available_agents: dict[str, str] = {
        "requester": "Performs HTTP requests; returns http data and response metadata",
        "authenticator": (
            "attempts to gain access to a web application by performing "
            "authentication (login, session fixation, credential stuffing)"
        ),
    }

    if sandbox_ok:
        available_agents["shell"] = (
            "executes bash commands in a sandbox and returns the output"
        )
        available_agents["python_interpreter"] = (
            "executes python code in a sandbox and returns the output"
        )

    if getattr(settings, "enable_webapp_analyzer", False):
        available_agents["webapp_analyzer"] = (
            "performs web application analysis, returns analysis and data"
        )

    available_agents["memory"] = (
        "stores temporary data and returns the status (temporary architecture "
        "storage of discovered knowledge)"
    )

    return available_agents


# ---------------------------------------------------------------------------
# 环境依赖：Sandbox / Embedder / RAG connector（复用原 pobi_agent.core）
# ---------------------------------------------------------------------------
def _prepare_env_dependencies(
    sandbox_manager: SandboxManager,
    session_id: str,
) -> tuple[Any, EmbedderClient, SqliteRagConnector]:
    """准备 ``DeadEndAgent.prepare_dependencies`` 所需的环境依赖。

    与原 pobi 一致：
    - ``embedder_client``：占位 ``EmbedderClient``（检索为可选能力）。
    - ``rag_connector``：``SqliteRagConnector``（按 session 隔离的本地向量库）。
    - ``sandbox``：真实 Docker 沙箱（每个任务独立容器）。
    """
    rag_manager = init_rag_session_manager()
    rag_connector = rag_manager.create_session(session_id=session_id)

    try:
        sandbox = sandbox_manager.create_sandbox()
    except Exception as exc:  # 沙箱是核心依赖，缺失时明确报错
        raise RuntimeError(
            "Docker 沙箱不可用，无法运行原 pobi DeadEndAgent（沙箱执行验证是核心能力）"
        ) from exc

    return sandbox, EmbedderClient(), rag_connector


# ---------------------------------------------------------------------------
# 主控：构建并运行 DeadEndAgent
# ---------------------------------------------------------------------------
async def run_deadend_agent(
    *,
    task: Task,
    target: Target,
    task_id: UUID,
    max_turns: int = 50,
) -> dict:
    """构建并驱动原 ``DeadEndAgent`` 完成一次完整的自主渗透测试。

    返回与原 ``ScanWorkflow.run`` 兼容的产出字典
    （``summary`` / ``confidence`` / ``structured_report`` / ``findings``），
    以便 ``executor.py`` 无需修改落库逻辑。
    """
    # 1) 多 LLM 模型规格（复用原 ModelSpec）
    model_spec = _build_model_spec(task.model or settings.model)

    # 2) 授权范围（写入全局 scope.yaml，复用原 ScopePolicy 闸门）
    _write_scope_file(target)

    # 3) 沙箱管理器 + 能力集合
    sandbox_manager = sandbox_setup()
    available_agents = _build_available_agents(sandbox_manager)

    # 4) 全局事件钩子（PobiV2EventHooks 已在应用启动时按 session_id==task_id 注册）
    hooks = get_event_hooks()
    # 5) 审批回调（接入 pobi_v2 高危工具审批，fail-closed）
    approval_cb = make_approval_callback(_async_session_factory(), request_id=task_id)

    # 6) 实例化原 DeadEndAgent（完整多智能体协作系统）
    agent = DeadEndAgent(
        session_id=task_id,
        model=model_spec,
        available_agents=available_agents,
        agents_storage_root=str(_agents_storage_root()),
        local_agent_id=None,  # 让 Config 自动分配/创建 local_agent_id
    )

    # 7) 设置目标（原 DeadEndAgent 内部通过 init_webtarget_indexer 绑定 target）
    agent.init_webtarget_indexer(target.url)

    # 8) 注入环境依赖（Sandbox / Embedder / RAG）—— 沙箱为必需
    sandbox, embedder_client, rag_connector = _prepare_env_dependencies(
        sandbox_manager, session_id=str(task_id)
    )
    agent.prepare_dependencies(
        embedder_client=embedder_client,
        rag_connector=rag_connector,
        sandbox=sandbox,
        target=target.url,
    )

    # 9) 注册审批回调
    agent.set_approval_callback(approval_cb)

    # 10) 逐阶段驱动（威胁建模 -> 利用 -> 报告）
    objective = task.objective or f"Perform a security assessment of {target.url}"

    # threat_model 返回 (task_node, reporter_output, validation_token)
    _, recon_report, _ = await agent.threat_model(task=objective)

    # run_exploitation 返回 (plan, validation_token)
    # 内部会：通过 SupervisorAgent 工具委派 6 个子 Agent 协作；
    # 在 Docker 沙箱中执行 shell / python 验证 payload；
    # 通过 ADaPT 递归分解任务；通过 ValidationGate(Flag+Judge) 验证目标达成。
    plan, validation_token = await agent.run_exploitation(
        threat_model=str(recon_report),
        task=objective,
    )

    # 11) 归一化为 executor 兼容的产出
    return _normalize_outcome(
        recon_report=recon_report,
        plan=plan,
        validation_token=validation_token,
    )


def _normalize_outcome(
    *,
    recon_report: Any,
    plan: Any,
    validation_token: str,
) -> dict:
    """把 DeadEndAgent 的产出（recon_report / plan / validation_token）归一化。"""
    summary_parts: list[str] = []
    structured_report: dict[str, Any] = {}
    findings: list[dict] = []
    confidence: Optional[float] = None

    if isinstance(recon_report, dict):
        summary_parts.append(str(recon_report.get("summary") or ""))
        structured_report.update(recon_report)
    elif isinstance(recon_report, str):
        summary_parts.append(recon_report)

    if plan is not None:
        if hasattr(plan, "confidence_score"):
            try:
                confidence = float(plan.confidence_score)
            except (TypeError, ValueError):
                confidence = None
        if hasattr(plan, "task"):
            summary_parts.append(f"规划任务: {plan.task}")

    summary = "\n\n".join(p for p in summary_parts if p).strip() or "扫描完成"
    structured_report.setdefault("summary", summary)
    if validation_token:
        structured_report["validation_token"] = validation_token

    return {
        "summary": summary,
        "confidence": confidence,
        "structured_report": structured_report,
        "findings": findings,
    }


# ---------------------------------------------------------------------------
# 辅助：agents 存储根目录 + async session 工厂
# ---------------------------------------------------------------------------
def _agents_storage_root() -> Path:
    root = Path(os.path.expanduser("~")) / ".pobi_v2" / "agents"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _async_session_factory():
    """延迟导入 pobi_v2 的 AsyncSessionLocal，避免循环依赖。"""
    from pobi_v2.db.session import AsyncSessionLocal

    return AsyncSessionLocal
