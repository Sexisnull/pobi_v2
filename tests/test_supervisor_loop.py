"""路径 A 测试：supervisor 决策器 + 驱动循环 + message_history 窗口化。

覆盖：
- window_messages 单元行为（保头、条数/token 双约束、空输入）
- SupervisorLoopConfig 刹车配置（request_limit 为唯一生效刹车）
- SupervisorDecision 决策模型
- 驱动循环：多轮委派后 complete 终止、history 有界、子 agent 输出仍经 _run_sub_agent
"""
from __future__ import annotations

import asyncio
import types
from unittest.mock import MagicMock

from pydantic_ai.usage import UsageLimits

from pobi_agent.agents.components.executor import (
    AgentExecutor as Executor,
    SupervisorLoopConfig,
)
from pobi_agent.agents.components.task_state import window_messages
from pobi_agent.agents.supervisor_agent import SupervisorDecision
from pobi_agent.config.settings import ModelSpec
from pobi_agent.context.context_engine import ContextEngine


# --------------------------------------------------------------------------- #
# 测试基建（fake infra）
# --------------------------------------------------------------------------- #
def _make_engine() -> ContextEngine:
    return ContextEngine(
        model=ModelSpec(provider="test", model_name="test-model", api_key=None, base_url=None),
        session_id="test-session",
        recon_store=None,
    )


def _make_fake_sub_agent_class(call_log: list):
    """fake 子 agent 类：替代 RequesterAgent/ShellAgent 等模块级类，记录委派调用且零真实 LLM。

    返回字符串 output（非 AgentOutput），使 call_* 跳过落库闭包，仅走 compact 结果路径。
    """

    class _FakeSubAgent:
        def __init__(self, *a, **k):
            pass

        async def run(self, prompt, **kwargs):
            call_log.append(prompt)
            return types.SimpleNamespace(output="sub-agent outcome")

    return _FakeSubAgent


class _SupervisorRunResult:
    def __init__(self, decision: SupervisorDecision):
        self.output = decision
        self.raw_messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "task"},
            {"role": "assistant", "content": decision.model_dump_json()},
        ]


def _make_fake_supervisor(decisions: list[SupervisorDecision]):
    class _F:
        def __init__(self, *a, **k):
            self._i = 0
            # .agent.tool 装饰器垫片：兼容 execute_supervisor 内保留的 @supervisor.agent.tool
            # （call_recon_lookup），避免 '_F' has no attribute 'agent'。
            self.agent = types.SimpleNamespace(tool=lambda f: f)

        async def run(self, prompt, **kwargs):
            d = decisions[min(self._i, len(decisions) - 1)]
            self._i += 1
            return _SupervisorRunResult(d)

    return _F


def _build_executor(monkeypatch, decisions: list[SupervisorDecision], call_log: list):
    """构造 Executor 并用 fake 替换 supervisor / 子 agent / 校验，避免真实 LLM 与 AVFS。

    - 子 agent 类（RequesterAgent/ShellAgent 等）替换为 fake：call_* 会真实委派并返回字符串，
      从而触发 call_log 追加且跳过落库闭包（无真实 DB/AVFS）。
    - deps 设为非空，使 call_* 的 early-return 检查不触发。
    """
    engine = _make_engine()
    executor = Executor(context=engine, model=ModelSpec(provider="test", model_name="test-model", api_key=None, base_url=None))
    # 子 agent 类 → fake（记录委派调用、零真实 LLM）；monkeypatch 模块级属性，
    # 因为 execute_supervisor 经模块全局名引用这些 agent 类。
    fake_cls = _make_fake_sub_agent_class(call_log)
    for name in (
        "RequesterAgent",
        "ShellAgent",
        "WebAppAnalyzerAgent",
        "PythonInterpreterAgent",
        "MemoryAgent",
        "AuthenticatorAgent",
    ):
        monkeypatch.setattr(
            f"pobi_agent.agents.components.executor.{name}", fake_cls, raising=False
        )
    executor.requester_deps = MagicMock()
    executor.shell_deps = MagicMock()
    # supervisor 决策器 → 返回预定决策序列
    monkeypatch.setattr(
        "pobi_agent.agents.components.executor.SupervisorAgent",
        _make_fake_supervisor(decisions),
    )
    # 跳过真实校验/上报（不影响驱动循环验证）
    async def _noop_validation(*a, **k):
        return None

    async def _noop_record(*a, **k):
        return None

    monkeypatch.setattr(executor, "_run_validation_and_report", _noop_validation)
    monkeypatch.setattr(executor, "_record_supervisor_result_for_validation_stop", _noop_record)
    return executor


def _collect(executor, node, agent_context, history):
    events = []

    async def _go():
        async for ev in executor.execute_supervisor(
            task_node=node, agent_context=agent_context, message_history=history
        ):
            events.append(ev)

    asyncio.run(_go())
    return events


# --------------------------------------------------------------------------- #
# window_messages 单元
# --------------------------------------------------------------------------- #
def test_window_messages_empty():
    assert window_messages([], 24, 6000) == []


def test_window_messages_keeps_head_and_bounds_messages():
    hist = [{"role": "user", "content": f"m{i}"} for i in range(50)]
    out = window_messages(hist, max_messages=10, max_tokens=100_000)
    assert len(out) <= 10
    assert out[0] == hist[0]  # 首条（任务锚点）永裁


def test_window_messages_token_budget_drops_middle():
    # 一条超大消息会迫使中间被丢弃，但首条仍在（按 token 预算，非字符长度）
    hist = [{"role": "user", "content": "head"}] + [
        {"role": "user", "content": "x" * 200} for _ in range(5)
    ]
    out = window_messages(hist, max_messages=100, max_tokens=60)
    assert out[0]["content"] == "head"
    from pobi_agent.utils.functions import num_tokens_from_string
    total = sum(num_tokens_from_string(m.get("content") or "") for m in out)
    assert total <= 60


# --------------------------------------------------------------------------- #
# SupervisorLoopConfig
# --------------------------------------------------------------------------- #
def test_supervisor_loop_config_usage_limits_bounded():
    cfg = SupervisorLoopConfig()
    # 轮次上限与请求预算解耦（P1-3）：for 边界 = max_rounds，usage 刹车 = request_limit
    assert cfg.max_rounds == 40
    assert cfg.request_limit == 80  # 40 轮决策 + 每轮至多 1 次工具调用的请求预算
    ul = cfg.usage_limits
    assert isinstance(ul, UsageLimits)
    assert ul.request_limit == 80
    assert ul.tool_calls_limit is None  # compat 层仅 request_limit 生效


# --------------------------------------------------------------------------- #
# SupervisorDecision
# --------------------------------------------------------------------------- #
def test_supervisor_decision_roundtrip():
    d = SupervisorDecision(action="call_agent", agent="requester", prompt="do x")
    assert d.action == "call_agent"
    assert d.agent == "requester"
    d2 = SupervisorDecision(action="complete", task_achieved=True, confidence_score=0.9)
    assert d2.action == "complete"
    assert d2.task_achieved is True


# --------------------------------------------------------------------------- #
# 驱动循环
# --------------------------------------------------------------------------- #
def test_driver_loop_iterates_then_completes(monkeypatch):
    decisions = [
        SupervisorDecision(action="call_agent", agent="requester", prompt="step1"),
        SupervisorDecision(action="call_agent", agent="shell", prompt="step2"),
        SupervisorDecision(action="complete", task_achieved=True, confidence_score=0.9,
                           detailed_summary="done", proofs="p"),
    ]
    call_log: list = []
    executor = _build_executor(monkeypatch, decisions, call_log)
    node = types.SimpleNamespace(task="test task", task_id="t1", status="in_progress")
    history: list = []
    events = _collect(executor, node, "ctx", history)

    # 子 agent 被委派 2 次（requester + shell）
    assert len(call_log) == 2
    # 终止后 history 有界（窗口化），不随轮次无界增长
    assert len(history) <= SupervisorLoopConfig().history_max_messages
    # 产出含置信度的 ResultEvent
    assert any(getattr(ev, "confidence_score", None) is not None for ev in events)


def test_driver_loop_history_bounded_under_many_rounds(monkeypatch):
    # 30 轮委派 + 最终 complete；request_limit=40 不触顶，但窗口化必须限制存储列表
    decisions = [SupervisorDecision(action="call_agent", agent="requester", prompt=f"step{i}") for i in range(30)]
    decisions.append(SupervisorDecision(action="complete", task_achieved=False, confidence_score=0.3))
    call_log: list = []
    executor = _build_executor(monkeypatch, decisions, call_log)
    node = types.SimpleNamespace(task="test task", task_id="t1", status="in_progress")
    history: list = []
    _collect(executor, node, "ctx", history)

    assert len(call_log) == 30
    # 存储列表经窗口化后必须 ≤ 上限（否则 L3 未根治）
    assert len(history) <= SupervisorLoopConfig().history_max_messages


def test_driver_loop_terminates_on_usage_limit_brake(monkeypatch):
    """UsageLimits 原生刹车（M-L3b）：框架在触顶时返回 FallbackAgentResult，
    驱动循环必须终止而非无限循环，并产出 ResultEvent。"""
    from pobi_agent.agents.factory import FallbackAgentResult, AgentOutput
    from pydantic_ai import UsageLimits

    # fake supervisor 首轮即返回 FallbackAgentResult（等价 UsageLimitExceeded 触发）
    class _FBrk:
        def __init__(self, *a, **k):
            self.agent = types.SimpleNamespace(tool=lambda f: f)

        async def run(self, prompt, **kwargs):
            return FallbackAgentResult(output=AgentOutput(), error="usage limit exceeded")

    # _build_executor 默认装入顺序决策器，此处覆盖为刹车型 supervisor
    monkeypatch.setattr(
        "pobi_agent.agents.components.executor.SupervisorAgent", _FBrk
    )
    call_log: list = []
    executor = _build_executor(monkeypatch, [], call_log)
    node = types.SimpleNamespace(task="test task", task_id="t1", status="in_progress")

    events = []

    async def _collect_brake():
        async for ev in executor.execute_supervisor(
            task_node=node,
            agent_context="ctx",
            message_history=[],
            usage_limits=UsageLimits(request_limit=1),
        ):
            events.append(ev)

    asyncio.run(_collect_brake())

    # 必须终止并产生 ResultEvent（不无限循环）
    result_events = [e for e in events if e.__class__.__name__ == "ResultEvent"]
    assert result_events, "usage limit 触发后必须产出 ResultEvent 并终止循环"
    assert len(call_log) == 0  # 未委派任何子 agent


# --------------------------------------------------------------------------- #
# P2 回归：role 序列净化 / 空决策防护
# --------------------------------------------------------------------------- #
def test_driver_loop_strips_trailing_tool_message(monkeypatch):
    """P2-1 回归：raw_messages 以 tool 结尾（CoreAgent 因 max_iterations 自然退出的
    场景）时，下一轮 supervisor.run 的 message_history 不得以 tool 结尾，
    否则 messages=[..., tool, user] 会触发 API 400。"""
    decisions = [
        SupervisorDecision(action="call_agent", agent="requester", prompt="step1"),
        SupervisorDecision(action="complete", task_achieved=True, confidence_score=0.9,
                           detailed_summary="done", proofs="p"),
    ]
    received: list = []

    class _ToolTailSup:
        def __init__(self, *a, **k):
            self._i = 0
            self.agent = types.SimpleNamespace(tool=lambda f: f)

        async def run(self, prompt, **kwargs):
            received.append(list(kwargs.get("message_history") or []))
            d = decisions[min(self._i, len(decisions) - 1)]
            self._i += 1
            return _ToolTailResult(d)

    class _ToolTailResult:
        def __init__(self, decision):
            self.output = decision
            # 末条为 tool 结果（无后续 assistant 配对）
            self.raw_messages = [
                {"role": "system", "content": "sys"},
                {"role": "user", "content": "task"},
                {"role": "assistant", "content": "calling",
                 "tool_calls": [{"function": {"name": "recon_lookup", "arguments": "{}"}}]},
                {"role": "tool", "name": "recon_lookup", "content": "{}"},
            ]

    call_log: list = []
    executor = _build_executor(monkeypatch, [], call_log)
    monkeypatch.setattr(
        "pobi_agent.agents.components.executor.SupervisorAgent", _ToolTailSup
    )
    node = types.SimpleNamespace(task="test task", task_id="t1", status="in_progress")
    _collect(executor, node, "ctx", [])

    assert len(received) == 2, f"supervisor 应被调用 2 轮，实际 {len(received)}"
    second_history = received[1]
    assert not second_history or second_history[-1].get("role") != "tool", (
        f"第二轮 message_history 以 tool 结尾（role 序列非法）: {second_history[-1]}"
    )
    assert call_log == ["step1"]  # 正常委派一次


def test_driver_loop_blocks_empty_agent_prompt(monkeypatch):
    """P2-3 回归：call_agent 决策缺 agent 或 prompt 时不得委派子 agent
    （空 prompt 会让子 agent 收到空任务乱跑），回灌错误提示由 supervisor 修正。"""
    decisions = [
        SupervisorDecision(action="call_agent", agent="requester", prompt=None),
        SupervisorDecision(action="call_agent", agent="", prompt="step-without-agent"),
        SupervisorDecision(action="complete", task_achieved=False, confidence_score=0.3,
                           detailed_summary="fixed", proofs=""),
    ]
    call_log: list = []
    executor = _build_executor(monkeypatch, decisions, call_log)
    node = types.SimpleNamespace(task="test task", task_id="t1", status="in_progress")
    events = _collect(executor, node, "ctx", [])

    assert call_log == [], f"缺 agent/prompt 的决策不应委派子 agent，实际委派 {len(call_log)} 次"
    result_events = [e for e in events if e.__class__.__name__ == "ResultEvent"]
    assert result_events, "应产出 ResultEvent 终止"
