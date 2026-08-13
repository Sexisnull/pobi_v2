"""pobi_v2 扫描工作流。

复刻原 `pobi_agent.pobi_agent.DeadEndAgent` 的三阶段扫描逻辑：
    1. threat_model      —— 生成侦察/攻击计划（复刻 `threat_model()`）
    2. supervisor loop    —— LLM 决策 + 工具调用（HTTP 侦察 / 受限 shell）的迭代执行，
                            每轮用 `ValidationGate` 判定是否达成目标
                            （复刻 `start_supervisor` / `run_exploitation` + validation_stop）
    3. report            —— 用 `ReporterAgent` 生成结构化报告（复刻原 reporter）

复用原 pobi_agent 的独立组件（直接 import，不重写逻辑）：
    - `pobi_agent.core_agent.CoreAgent`          —— LLM 驱动（含完整 EventHooks 兼容）
    - `pobi_agent.scope.ScopePolicy`             —— 授权范围闸门（见 scan_tools）
    - `pobi_agent.agents.components.validation_strategies.ValidationGate`
                                                    —— confidence / validation 判定
    - `pobi_agent.agents.reporter.ReporterAgent` —— 报告生成

pobi_v2 自行实现的部分（编排外壳 + HTTP/shell 工具），原因：
    原 `DeadEndAgent` 强依赖浏览器自动化(Playwright)、Docker 沙箱、AVFS、RAG 等
    设施，无法在 pobi_v2 的 FastAPI 服务中直接实例化；此处以等价语义重新编排，
    但核心判定/闸门/报告逻辑全部复用原代码，保证行为一致。

白盒代码分析（code_indexer）接入策略：
    `pobi_agent.code_indexer.SourceCodeIndexer` 依赖 Playwright 抓取、Embedder
    后端与 RAG 存储，属重量级可选能力。pobi_v2 以「可插拔、懒加载、依赖缺失即降级」
    的方式预留接入点 `resolve_whitebox_stage`：默认关闭，开启且依赖齐备时作为
    白盒分析阶段注入工作流；依赖缺失（典型部署环境）则优雅跳过，不阻断主流程。
"""
from __future__ import annotations

import json
from typing import Any, Callable
from urllib.parse import urlparse

from pobi_agent.agents.components.validation_strategies import (
    FlagStrategy,
    JudgeAgentStrategy,
    ValidationGate,
    ValidationInput,
)
from pobi_agent.agents.reporter import ReporterAgent
from pobi_agent.config.settings import ModelSpec
from pobi_agent.core_agent import CoreAgent
from pobi_agent.hooks import EventHooks

from pobi_v2.core.config import settings
from pobi_v2.engine.scan_tools import ToolContext, build_scope_policy, http_request, run_shell
from pobi_v2.llm import get_model_spec, to_litellm_model


def _out_summary(output: Any) -> str:
    s = getattr(output, "detailed_summary", None)
    if s:
        return str(s)
    return str(output) if isinstance(output, str) else ""


def _out_confidence(output: Any) -> float:
    try:
        return float(getattr(output, "confidence_score", None) or 0.0)
    except (TypeError, ValueError):
        return 0.0


_HIGH_RISK = {
    "run_shell", "shell", "execute_command", "reverse_shell", "exploit",
    "sql_injection", "command_injection", "xss_exploit", "brute_force",
}


def _is_high_risk(tool_name: str) -> bool:
    return tool_name.lower() in _HIGH_RISK


def _parse_decision(text: str) -> dict:
    """从 LLM 输出中解析工具调用 / 完成决策（容错）。"""
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except Exception:
            pass
    return {}


def _extract_root_domains(url: str) -> list[str]:
    """从目标串提取根域名（授权范围用）。

    仅当串具备 URL 形态（含 scheme:// 或 netloc）或纯主机名形态时才返回，
    否则（如自然语言描述 "not a url"）返回空列表，避免把非目标误纳入 scope。
    """
    import re

    cleaned = (url or "").strip()
    if not cleaned:
        return []
    parsed = urlparse(cleaned)
    netloc = parsed.netloc
    # 无 scheme 且无 netloc：可能是纯主机名，需校验形态（禁止含空格/路径符/中文等）
    if not parsed.scheme and not netloc:
        if not re.fullmatch(r"[A-Za-z0-9.\-_]+", cleaned):
            return []
        netloc = cleaned
    netloc = netloc.split("@")[-1].split(":")[0]
    return [netloc] if netloc else []


# 模型规格统一由 pobi_v2.llm.get_model_spec 解析（消除主/降级路径分叉）。


def _make_core_agent(model: str) -> CoreAgent:
    """复刻核心 LLM 代理构造（与原 CoreAgent 一致的 instructions 约束）。

    模型解析统一走 pobi_v2.llm 入口，凭证（POBI_V2_LLM_API_KEY 优先、裸供应商
    变量兜底）一并注入，确保降级路径不再读不到平台统一前缀凭证。
    """
    spec = get_model_spec(model)
    return CoreAgent(
        model=to_litellm_model(spec),
        instructions=(
            "你是一名严谨的 Web 安全渗透测试助手，遵循授权范围，"
            "禁止猜测，所有结论需基于真实工具返回的数据。"
        ),
        api_key=spec.api_key,
        api_base=spec.base_url,
    )


class ScanWorkflow:
    """复刻 DeadEndAgent 的扫描编排。"""

    def __init__(
        self,
        target: Any,  # pobi_v2.db.models.Target
        task: Any,  # pobi_v2.db.models.Task
        hooks: EventHooks,
        approval_callback: Callable | None = None,
        model: str | None = None,
        max_turns: int = 50,
        allow_shell: bool = False,
    ) -> None:
        self.target = target
        self.task = task
        self.hooks = hooks
        self.approval_callback = approval_callback
        self.model = model or settings.model
        self.max_turns = max_turns
        self.allow_shell = allow_shell
        self.session_id = str(task.id)

        # 从 pobi_v2 Target 解析 scope（复刻原 RequesterDeps.target 角色）
        root_domains = _extract_root_domains(target.url)
        self.scope = build_scope_policy(
            root_domains=root_domains,
            in_scope=list(target.in_scope or []),
            out_of_scope=list(target.out_of_scope or []),
            enabled=True,
        )
        self.tool_ctx = ToolContext(
            scope=self.scope,
            agent_id="deadend",
            session_id=self.session_id,
            allow_shell=allow_shell,
            out_of_scope_raw=list(target.out_of_scope or []),
        )

        # 复用原 ValidationGate（FlagStrategy 确定性 + JudgeAgentStrategy LLM 判定）
        model_spec = get_model_spec(self.model)
        self.validation_gate = ValidationGate(
            strategies=[FlagStrategy(), JudgeAgentStrategy(model_spec)]
        )
        # 复用原 ReporterAgent（依赖 ModelSpec；报告写到 AVFS，pobi_v2 环境回退到结构化汇总）
        self.reporter = ReporterAgent(model_spec)

        # 工作记忆（复刻原 context_engine 的 session 级累积）
        self.memory: list[str] = []

    # ---------------- 阶段 1：威胁建模 / 计划 ----------------
    async def threat_model(self) -> str:
        self.hooks.emit_agent_start(
            session_id=self.session_id,
            agent_name="planner",
            task=f"threat_model:{self.target.url}",
        )
        prompt = (
            "你是一名渗透测试规划助手。基于下列授权目标，输出一份简洁的侦察与"
            "漏洞评估计划，列出优先执行的步骤（被动侦察、主动侦察、已知漏洞利用、"
            "认证绕过等），并说明每一步的目的。\n\n"
            f"目标 URL: {self.target.url}\n"
            f"授权范围: {json.dumps(self.target.in_scope, ensure_ascii=False)}\n"
            f"排除范围: {json.dumps(self.target.out_of_scope, ensure_ascii=False)}\n"
            f"任务目标: {self.task.objective}\n"
        )
        out = await _make_core_agent(self.model).run(prompt=prompt, deps=None)
        plan = _out_summary(out)
        self.hooks.emit_agent_thought(
            session_id=self.session_id, agent_name="planner", thought=plan[:500]
        )
        self.memory.append(f"[PLAN] {plan}")
        return plan

    # ---------------- 工具调度（复用 ScopePolicy 出口闸门） ----------------
    async def _dispatch_tool(self, tool_name: str, tool_args: dict) -> dict:
        """执行一次工具调用，并可能经审批 gate。

        复刻原 `AgentExecutor` 的高危工具拦截 + `pw_requester` 的 scope 闸门。
        """
        if self.approval_callback and _is_high_risk(tool_name):
            decision = await self._request_approval(tool_name, tool_args)
            if decision != "approve":
                return {"error": "rejected_by_approval"}

        self.hooks.emit_tool_call_start(
            session_id=self.session_id,
            agent_name="deadend",
            tool_name=tool_name,
            args=json.dumps(tool_args, ensure_ascii=False),
        )
        try:
            if tool_name in ("http_request", "http_get", "fetch"):
                result = await http_request(
                    self.tool_ctx,
                    method=(tool_args.get("method") or "GET").upper(),
                    url=tool_args["url"],
                    headers=tool_args.get("headers"),
                    body=tool_args.get("body"),
                )
            elif tool_name in ("run_shell", "shell", "execute_command"):
                result = await run_shell(self.tool_ctx, tool_args["command"])
            else:
                result = {"error": f"unknown tool: {tool_name}"}
        except Exception as exc:  # 越权 ScopeViolation 等
            result = {"error": str(exc)}
        self.hooks.emit_tool_call_end(
            session_id=self.session_id,
            agent_name="deadend",
            tool_name=tool_name,
            success="error" not in result,
            result=json.dumps(result, ensure_ascii=False)[:2000],
        )
        return result

    async def _request_approval(self, tool_name: str, tool_args: dict) -> str:
        if not self.approval_callback:
            return "approve"
        decision = await self.approval_callback(tool_name, tool_args)
        self.hooks.emit_agent_thought(
            session_id=self.session_id,
            agent_name="approval",
            thought=f"高危工具 {tool_name} 审批结果: {decision}",
        )
        return decision

    # ---------------- 阶段 2：supervisor 执行循环 ----------------
    async def run_exploitation(self) -> tuple[float, str, str]:
        """复刻 `start_supervisor` + `run_exploitation` 的迭代循环。

        返回 (best_confidence, final_summary, evidence)。
        """
        goal = self.task.objective
        best_confidence = 0.0
        evidence = ""

        system = (
            "你是一名执行渗透测试任务的 AI 代理。你可以调用以下工具获取证据：\n"
            "- http_request(method, url, headers?, body?)：发起 HTTP 侦察（受授权范围约束）\n"
            "- run_shell(command)：执行受限的只读探测命令（默认禁用）\n\n"
            "请逐步推理，先侦察再深入，基于真实返回数据得出结论。"
            "当需要调用工具时，请严格以 JSON 形式输出：\n"
            '{"tool": "http_request", "args": {"method": "GET", "url": "..."}}\n'
            "若已得出关于任务目标的结论，请输出：\n"
            '{"done": true, "confidence": 0.0-1.0, "summary": "...", "proofs": "..."}'
        )

        context = "\n".join(self.memory[-10:])
        for i in range(self.max_turns):
            self.hooks.emit_agent_start(
                session_id=self.session_id,
                agent_name="supervisor",
                task=f"iteration:{i+1}",
            )
            prompt = (
                f"任务目标: {goal}\n\n已有上下文:\n{context}\n\n"
                "下一步执行（输出工具调用 JSON 或 done JSON）："
            )
            out = await _make_core_agent(self.model).run(
                instructions=system, prompt=prompt, deps=None
            )
            text = _out_summary(out)
            decision = _parse_decision(text)
            if decision.get("done"):
                conf = float(decision.get("confidence", 0.0))
                summary = decision.get("summary", "")
                proofs = decision.get("proofs", "")
                self.hooks.emit_agent_thought(
                    session_id=self.session_id,
                    agent_name="supervisor",
                    thought=f"声明完成 conf={conf:.2f}",
                )
                # 复用原 ValidationGate 做一致性校验（复刻 validation_stop）
                vinput = ValidationInput(
                    task_achieved=True,
                    detailed_summary=summary,
                    proofs=proofs,
                    confidence_score=conf,
                )
                verdict = await self.validation_gate.check(
                    vinput, root_goal=goal, context=context
                )
                self.hooks.emit_validation_result(
                    session_id=self.session_id,
                    task="validate",
                    task_id="root",
                    valid=bool(verdict.stop),
                    confidence_score=verdict.confidence,
                    critique=str(verdict.critique)[:300],
                )
                best_confidence = max(best_confidence, verdict.confidence)
                evidence = proofs
                if verdict.stop:  # 达成目标 → 终止（复刻 validation_stop）
                    self.memory.append(
                        f"[RESULT] {summary} (conf={verdict.confidence:.2f})"
                    )
                    return best_confidence, summary, evidence
                context += f"\n[ITER {i+1}] {summary} (validation: {verdict.critique})"
                continue

            # 工具调用分支
            tool_name = decision.get("tool")
            tool_args = decision.get("args", {})
            result = await self._dispatch_tool(tool_name, tool_args)
            self.memory.append(
                f"[ITER {i+1}] tool={tool_name} -> "
                f"{json.dumps(result, ensure_ascii=False)[:800]}"
            )
            context += (
                f"\n[ITER {i+1}] {tool_name} 返回: "
                f"{json.dumps(result, ensure_ascii=False)[:800]}"
            )

        return best_confidence, text, evidence

    # ---------------- 阶段 3：报告 ----------------
    async def report(self, verdict) -> tuple[str, dict]:
        """复刻原 `ReporterAgent.summarize_and_write`。"""
        context = "\n".join(self.memory)
        self.hooks.emit_agent_start(
            session_id=self.session_id,
            agent_name="reporter",
            task="report",
        )
        self.hooks.emit_agent_thought(
            session_id=self.session_id,
            agent_name="reporter",
            thought="汇总发现并生成报告…",
        )
        structured = {
            "target": self.target.url,
            "objective": self.task.objective,
            "summary": context[-2000:],
            "confidence": getattr(verdict, "confidence", 0.0),
        }
        try:
            # 复用原 ReporterAgent（依赖 LLM + AVFS；AVFS 缺失时回退）
            await self.reporter.summarize_and_write(
                root_goal=self.task.objective,
                verdict=verdict,
                context=context,
                session_id=self.session_id,
            )
        except Exception as exc:
            structured["reporter_error"] = str(exc)
        self.hooks.emit_agent_end(
            session_id=self.session_id,
            agent_name="reporter",
            task="report",
            confidence_score=getattr(verdict, "confidence", 0.0),
        )
        return "", structured

    # ---------------- 总入口 ----------------
    async def run(self) -> dict:
        self.hooks.emit_agent_start(
            session_id=self.session_id,
            agent_name="deadend",
            task=self.task.objective,
        )
        await self.threat_model()
        conf, summary, evidence = await self.run_exploitation()
        # 用最终置信度构造 verdict 供报告生成（复刻原 validation → report 链路）
        from pobi_agent.agents.components.validation_strategies import ValidationVerdict

        verdict = ValidationVerdict(stop=True, confidence=conf, token=evidence)
        path, structured = await self.report(verdict)
        self.hooks.emit_agent_end(
            session_id=self.session_id,
            agent_name="deadend",
            task=self.task.objective,
            confidence_score=conf,
        )
        return {
            "confidence": conf,
            "summary": summary,
            "evidence": evidence,
            "report_path": path,
            "structured_report": structured,
        }


# ---------------- 白盒阶段扩展点（可选能力） ----------------

class WhiteBoxStage:
    """白盒代码分析阶段封装（可选）。

    懒加载 `pobi_agent.code_indexer.SourceCodeIndexer`，将其作为三阶段之外的
    可选前置/并行分析阶段。依赖（Playwright / Embedder 后端 / RAG）缺失时，
    通过 `resolve_whitebox_stage` 降级为 None，不阻断主流程。
    """

    def __init__(self, indexer) -> None:
        self._indexer = indexer

    async def analyze(self, target_url: str) -> dict:
        """对目标 URL 执行白盒代码索引，返回结构化摘要。

        依赖缺失或执行失败时显式抛错，由调用方决定降级策略。
        """
        await self._indexer.crawl_target()
        return {"stage": "whitebox", "target": target_url, "status": "indexed"}


async def resolve_whitebox_stage(enabled: bool = False):
    """解析白盒分析阶段实例，依赖缺失时优雅降级为 None。

    设计原则（AGENTS.md：最少依赖 / 显式错误）：
    - `enabled=False`：直接返回 None，不触碰重依赖。
    - `enabled=True` 但 `SourceCodeIndexer` 或其依赖（tree_sitter / embedder /
      Playwright）不可用：捕获 ImportError 并降级为 None，附警告日志，不中断流程。
    - 依赖齐备：返回 `WhiteBoxStage` 实例，由上层在扫描编排中按需在黑盒阶段前注入。
    """
    if not enabled:
        return None
    try:
        from pobi_agent.code_indexer.code_indexer import SourceCodeIndexer

        # 仅验证类可导入；真实实例化推迟到 analyze()（需 crawl target）
        return WhiteBoxStage(SourceCodeIndexer)
    except ImportError as exc:
        from pobi_agent.logging import logger

        logger.warning(
            "白盒阶段已启用，但依赖不可用（%s）；降级跳过，仅执行黑盒扫描。",
            exc,
        )
        return None

