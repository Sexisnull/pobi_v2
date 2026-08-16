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

import asyncio
import os
import logging
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
from pobi_agent.agents.components.validation_strategies import (
    DEADEND_VALIDATION_CONFIG_PATH,
)

from pobi_v2.core.config import settings
from pobi_v2.db.models import Task, Target
from pobi_v2.engine.approval import make_approval_callback
from pobi_v2.engine.event_bus import bus, MemoryEventBusBackend, persist_event_worker
from pobi_v2.llm import get_model_spec, to_litellm_model


# 内存事件总线下，worker 进程需自行启动持久化协程；Redis 由 FastAPI 统一消费。
_local_persist_started = False


def _ensure_local_persist_worker() -> None:
    global _local_persist_started
    if _local_persist_started:
        return
    if isinstance(bus, MemoryEventBusBackend):
        asyncio.create_task(persist_event_worker())
    _local_persist_started = True


# ---------------------------------------------------------------------------
# 授权范围：把 pobi_v2 的 Target 写入 scope.{task_id}.yaml，复用原 ScopePolicy 闸门
# ---------------------------------------------------------------------------
def _write_scope_file(target: Target, task_id: UUID) -> Path:
    """把 pobi_v2 的 Target 范围写入按任务隔离的 scope.{task_id}.yaml。

    原 pobi 的授权闸门在 ``pw_requester`` 网络出口处通过 ``check_scope`` 读取
    该文件。多任务并发时，每个任务写独立文件（不再覆盖全局 scope.yaml），
    从而消除全局单例被并发覆盖导致的越权扫描。文件缺失时内核回退为安全禁用策略。
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

    # 原 pobi 的 ScopePolicy 仅支持 host/IP 级排除：它会对 out_of_scope 条目做
    # _normalize_host，把带 path 的 URL（如 http://host/path）归一化为裸 host 并
    # 加入 deny 列表，从而把整台主机（含 in-scope 的根路径）误排除。因此这里只
    # 保留纯 host/IP 级条目，path 级排除交由 LLM objective 约束，避免误伤。
    _out_raw = list(target.out_of_scope or [])
    out_of_scope = [
        o for o in _out_raw
        if o and "/" not in o.split("://", 1)[-1]
    ]

    scope_doc: dict[str, Any] = {
        "enabled": bool(target.enabled and (target.in_scope or target.url)),
        "root_domains": root_domains,
        "domains": [],
        "ips": [],
        "out_of_scope": out_of_scope,
        "max_qps": getattr(settings, "scope_max_qps", 10),
        "max_bytes": getattr(settings, "scope_max_bytes", 5_000_000),
    }

    scope_path = SCOPE_YAML_PATH.parent / f"scope.{task_id}.yaml"
    scope_path.parent.mkdir(parents=True, exist_ok=True)
    scope_path.write_text(
        yaml.safe_dump(scope_doc, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    return scope_path


# ---------------------------------------------------------------------------
# 验证策略：把 Target 的 Validation Configuration 写入 validation.{task_id}.yaml
# ---------------------------------------------------------------------------
def _write_validation_config(target: Target, task_id: UUID) -> Path:
    """把授权目标的验证策略写入按任务隔离的 validation.{task_id}.yaml。

    原 pobi 的 ValidationGate(Flag+Judge) 在运行时通过 ``load_validation_config``
    读取该文件，从而决定「怎样才算找到漏洞」（flag 正则、judge LLM、信心阈值）。
    多任务并发时每个任务写独立文件，消除全局 validation.yaml 被并发覆盖的缺陷。
    """
    strategies: list[dict[str, Any]] = []
    if target.flag_regex:
        strategies.append({"name": "flag", "pattern": target.flag_regex})
    # judge 始终启用（LLM  судья 兜底验证），与 deadend-cli 默认一致
    judge_block: dict[str, Any] = {"name": "judge"}
    if target.validation_format:
        judge_block["validation_format"] = target.validation_format
    strategies.append(judge_block)

    validation_doc: dict[str, Any] = {
        "validation_format": target.validation_format or "FLAG{}",
        "validation_type": "flag" if target.flag_regex else "security assessment",
        "strategies": strategies,
        # 信心阈值带与任务树深度作为注释级元信息写入，供人工审阅；
        # 阈值核心调度走 Task.agent 参数，深度约束走 max_turns 上限。
        "_confidence_threshold": target.confidence_threshold,
        "_max_tree_depth": target.max_tree_depth,
    }

    validation_path = DEADEND_VALIDATION_CONFIG_PATH.parent / f"validation.{task_id}.yaml"
    validation_path.parent.mkdir(parents=True, exist_ok=True)
    validation_path.write_text(
        yaml.safe_dump(validation_doc, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    return validation_path


# ---------------------------------------------------------------------------
# 可用 Agent 能力集合（与 jsonrpc_server._build_available_agents 保持一致）
# ---------------------------------------------------------------------------
def _build_available_agents(
    sandbox_manager: SandboxManager,
) -> dict[str, str]:
    """构造 ``DeadEndAgent`` 的 ``available_agents`` 能力字典。

    共享 Kali 沙箱为**强依赖**（侦查与验证阶段均使用，执行 shell 与 python），
    必须随 compose 常驻启动。本函数复用全局共享 Kali 单例；若容器缺失则明确
    抛错，不再静默降级（避免能力被无声关闭而隐藏沙箱故障）。
    - ``shell`` / ``python_interpreter`` 始终提供（沙箱为核心能力）。
    - ``webapp_analyzer`` 仅在启用浏览器分析时提供。
    """
    # 共享 Kali 为强依赖：解析常驻单例，缺失即抛错，绝不调用 create_sandbox
    # 新建临时容器（每次运行泄漏孤儿沙箱）。
    sandbox = sandbox_manager.get_or_create_shared_kali()
    if sandbox is None:
        raise RuntimeError(
            "Docker 沙箱不可用，无法暴露 shell / python_interpreter 能力"
            "（共享 Kali 为强依赖，须随 compose 常驻启动）"
        )

    available_agents: dict[str, str] = {
        "requester": "Performs HTTP requests; returns http data and response metadata",
        "authenticator": (
            "attempts to gain access to a web application by performing "
            "authentication (login, session fixation, credential stuffing)"
        ),
        "shell": (
            "executes bash commands in a sandbox and returns the output"
        ),
        "python_interpreter": (
            "executes python code in a sandbox and returns the output"
        ),
    }

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
) -> tuple[Any, EmbedderClient]:
    """准备 ``DeadEndAgent.prepare_dependencies`` 所需的运行期环境依赖。

    与原 pobi 一致：
    - ``embedder_client``：``EmbedderClient``（向量检索依赖，外部 LLM API）。
    - ``sandbox``：真实 Docker 沙箱（全面容器化后复用 compose 常驻的全局
      共享 Kali 容器，shell 命令与 Python 验证共用同一容器，而非每任务独立容器）。

    RAG 连接器（``SqliteRagConnector``）此处不准备：它按 ``(agent_id,
    session_id, target)`` 隔离，需要 ``DeadEndAgent`` 实例化后才能拿到
    ``agent.agent_id``，故在 ``run_deadend_agent`` 内、agent 实例化之后获取。
    """
    try:
        # 复用全局共享 Kali 容器（单实例），缺失时按统一网络创建。
        sandbox = sandbox_manager.get_or_create_shared_kali()
    except Exception as exc:  # 沙箱是核心依赖，缺失时明确报错
        raise RuntimeError(
            "Docker 沙箱不可用，无法运行原 pobi DeadEndAgent（沙箱执行验证是核心能力）"
        ) from exc

    # EmbedderClient 依赖外部 LLM API（生成 embedding），初始化失败仅告警降级，
    # 不阻断主流程（检索为增强能力）。凭证统一走 pobi_v2.llm 解析入口，
    # 保证 POBI_V2_LLM_API_KEY 与模型路径行为一致。
    embedder_client = None
    try:
        _embed_spec = get_model_spec()
        # 注意：vector_dim 不在此硬编码，交由 EmbedderClient 按模型默认维度处理
        # （registry.py 默认 1536）。维度须与实际 embedding 模型一致，否则
        # similarity_search_code_chunk 会因维度过滤剔除全部 chunk 而检索恒空。
        # 若后续正式启用向量检索，应从 EmbeddingSpec.vec_dim 动态读取并显式传入。
        embedder_client = EmbedderClient(
            model_name=to_litellm_model(_embed_spec),
            api_key=_embed_spec.api_key,
            base_url=_embed_spec.base_url,
            vector_dim=None,
        )
    except Exception as exc:  # noqa: BLE001
        logging.warning("EmbedderClient 初始化失败，降级为 None：%s", exc)
        embedder_client = None

    return sandbox, embedder_client


async def _prepare_rag_connector(
    agent_id: UUID,
    session_id: str,
    target: str,
) -> SqliteRagConnector:
    """获取本任务的 RAG 连接器（按 agent/session 隔离的本地 SQLite 向量库）。

    RAG 默认开启、零外部依赖（SQLite + numpy 本地检索），不降级为 None：
    初始化失败即明确报错，避免静默丢失检索能力。
    """
    rag_manager = init_rag_session_manager()
    connector = await rag_manager.get_connector(
        agent_id=agent_id,
        embedding_session_id=session_id,
        target=target,
    )
    return connector


# ---------------------------------------------------------------------------
# 主控：构建并运行 DeadEndAgent
# ---------------------------------------------------------------------------
async def run_deadend_agent(
    *,
    task: Task,
    target: Target,
    task_id: UUID,
    max_turns: int = 50,
    auto_approve: bool = False,
) -> dict:
    """构建并驱动原 ``DeadEndAgent`` 完成一次完整的自主渗透测试。

    返回与原 ``ScanWorkflow.run`` 兼容的产出字典
    （``summary`` / ``confidence`` / ``structured_report`` / ``findings``），
    以便 ``executor.py`` 无需修改落库逻辑。
    """
    # 1) 多 LLM 模型规格（统一入口产出内核 ModelSpec）
    model_spec = get_model_spec(task.model or settings.model)

    # 2) 授权范围（写入 scope.{task_id}.yaml，按任务隔离，复用原 ScopePolicy 闸门）
    # 该文件由内核 pw_requester 按 session_id 命名约定读取，无需返回值。
    _write_scope_file(target, task_id)

    # 2.5) 验证策略（写入 validation.{task_id}.yaml，按任务隔离，复用原 ValidationGate(Flag+Judge)）
    validation_path = _write_validation_config(target, task_id)

    # 3) 沙箱管理器 + 能力集合
    sandbox_manager = sandbox_setup()
    available_agents = _build_available_agents(sandbox_manager)

    # 4) 全局事件钩子（PobiV2EventHooks 已在应用启动时按 session_id==task_id 注册）
    hooks = get_event_hooks()
    # 5) 审批回调（接入 pobi_v2 高危工具审批；yolo 模式免人工审批）
    approval_cb = make_approval_callback(
        _async_session_factory(),
        tenant_id=task.tenant_id,
        task_id=task_id,
        auto_approve=auto_approve,
    )

    # 6) 实例化原 DeadEndAgent（完整多智能体协作系统）
    agent = DeadEndAgent(
        session_id=task_id,
        model=model_spec,
        available_agents=available_agents,
        agents_storage_root=str(_agents_storage_root()),
        local_agent_id=None,  # 让 Config 自动分配/创建 local_agent_id
        validation_config_path=str(validation_path),
    )

    # 7) 设置目标（原 DeadEndAgent 内部通过 init_webtarget_indexer 绑定 target）
    agent.init_webtarget_indexer(target.url)

    # 8) 注入环境依赖（Sandbox / Embedder / RAG）—— 沙箱为必需
    # RAG 连接器需在 agent 实例化后获取（需 agent.agent_id），默认开启不降级。
    sandbox, embedder_client = _prepare_env_dependencies(sandbox_manager)
    rag_connector = await _prepare_rag_connector(
        agent_id=agent.agent_id,
        session_id=str(task_id),
        target=target.url,
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
    _ensure_local_persist_worker()
    objective = task.objective or f"Perform a security assessment of {target.url}"

    hooks.emit_phase_changed(task_id, "initialization", detail="任务启动，准备环境")

    # threat_model 返回 (task_node, reporter_output, validation_token)
    hooks.emit_phase_changed(task_id, "reconnaissance", detail="开始威胁建模与信息收集")
    _, recon_report, _ = await agent.threat_model(task=objective)
    recon_report = recon_report or ""

    # run_exploitation 返回 (plan, validation_token)
    # 内部会：通过 SupervisorAgent 工具委派 6 个子 Agent 协作；
    # 在 Docker 沙箱中执行 shell / python 验证 payload；
    # 通过 ADaPT 递归分解任务；通过 ValidationGate(Flag+Judge) 验证目标达成。
    hooks.emit_phase_changed(task_id, "exploitation", detail="开始利用与验证")
    plan, validation_token = await agent.run_exploitation(
        threat_model=str(recon_report),
        task=objective,
    )

    hooks.emit_phase_changed(task_id, "reporting", detail="生成最终报告")

    # 最终安全评估报告正文：利用阶段达成目标后由 ReporterAgent 写入
    # self.stop_result.reporter_output（对齐 deadend-cli CLI 的 report 产物）。
    report = ""
    stop_result = getattr(agent, "stop_result", None)
    if stop_result is not None and getattr(stop_result, "reporter_output", None):
        report = stop_result.reporter_output

    # 实时推送安全评估报告到前端聊天流（前端已具备 report_task_event 渲染分支，
    # 此前后端漏发该事件，导致报告仅落库、UI 不展示）。
    if report:
        hooks.emit_report(task_id=str(task.id), summary=report)
    else:
        hooks.emit_log_message(
            session_id=str(task.id),
            message="未生成安全评估报告正文（ReporterAgent 无输出），请通过「查看报告」查阅结构化产物。",
            level="warn",
            source="reporting",
        )

    # 11) 归一化为 executor 兼容的产出
    return _normalize_outcome(
        recon_report=recon_report,
        plan=plan,
        validation_token=validation_token,
        report=report,
    )


def _normalize_outcome(
    *,
    recon_report: Any,
    plan: Any,
    validation_token: str,
    report: str | None = None,
) -> dict:
    """把 DeadEndAgent 的产出（recon_report / plan / validation_token / report）归一化。"""
    summary_parts: list[str] = []
    structured_report: dict[str, Any] = {}
    findings: list[dict] = []
    confidence: Optional[float] = None

    if isinstance(recon_report, dict):
        summary_parts.append(str(recon_report.get("summary") or ""))
        structured_report.update(recon_report)
    elif isinstance(recon_report, str):
        summary_parts.append(recon_report)

    if report:
        summary_parts.append(report)
        structured_report["report"] = report

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
