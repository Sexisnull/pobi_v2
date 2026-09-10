"""外层 ADaPT 循环回归测试（P0-1 / P0-2 修复）。

覆盖（test_supervisor_loop.py 只测内层驱动循环，以下为外层 while 的回归用例）：
- P0-1：子任务触发 ValidationStopEvent（exit_loop=True）后，父节点不得继续迭代执行 executor；
- P0-2：supervisor 恒处于 expand / refine 区间（conf 不收敛）时，外层 while 迭代受
  MAX_TASK_ATTEMPTS=3 约束后 failed:max_attempts 终止，不再无限迭代。
"""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from pobi_agent.agents.components.planner import TaskNode
from pobi_agent.agents.components.executor import (
    ResultEvent,
    LogEvent,
    ValidationStopEvent,
)
from pobi_agent.agents.recon_threatmodel_agent import GeneralInfoOutput
from pobi_agent.agents.exploit_web_agent import ExploitInfo
from pobi_agent.hooks import NullEventHooks
import pobi_agent.agents.architecture as arch


@pytest.fixture(autouse=True)
def _null_hooks(monkeypatch):
    """屏蔽事件钩子，避免测试依赖外部基础设施。"""
    monkeypatch.setattr(arch, "get_event_hooks", lambda: NullEventHooks())


def _make_node(task: str, depth: int = 1) -> TaskNode:
    return TaskNode(
        task=task, depth=depth, confidence_score=0.7,
        status="in_progress", parent=None,
    )


class FakeExecutor:
    """mock executor：supervisor 恒定输出，可计数真实执行次数。"""

    def __init__(self, conf: float, sub_triggers_validation: bool = False):
        self.conf = conf
        self.sub_triggers_validation = sub_triggers_validation
        self.calls = 0

    async def execute_supervisor(self, task_node, agent_context="", **kw):
        self.calls += 1
        yield LogEvent(message=f"[FAKE-EXEC] call#{self.calls} task={task_node.task!r}")
        if self.sub_triggers_validation and task_node.task.startswith("sub"):
            yield ValidationStopEvent(
                validation_token="tok", confidence_score=0.9,
                critique="done", reporter_output="rep",
            )
        else:
            yield ResultEvent(confidence_score=self.conf, context={
                "task_achieved": False, "detailed_summary": "partial", "proofs": "",
                "supervisor_response": "", "supervisor_history": agent_context or "", "log": "",
            })


class FakePlanner:
    """mock planner：expand 生成 1 个子任务（同文本），update_plan 返回同文本任务（同 hash）。"""

    async def expand(self, parent_task, context, usage, usage_limits):
        sub = TaskNode(
            task=f"sub:{parent_task.task}", depth=parent_task.depth + 1,
            confidence_score=0.3, status="pending", parent=parent_task,
        )
        return [sub], GeneralInfoOutput(), ExploitInfo()

    async def update_plan(self, task, context, usage, usage_limits):
        new_task = TaskNode(
            task=task.task, node_id=task.node_id, depth=task.depth,
            confidence_score=0.3, status="refine", parent=task.parent,
        )
        return [new_task], GeneralInfoOutput(), ExploitInfo()


def _make_adapt(conf: float, sub_triggers_validation: bool = False):
    executor = FakeExecutor(conf, sub_triggers_validation)
    adapt = arch.ADaPTAgent(
        context=MagicMock(), executor=executor, planner=FakePlanner(), max_depth=2,
    )
    return adapt, executor


def _collect(adapt, node):
    """完整消费 _solve 的异步生成器。"""
    chunks: list = []

    async def _go():
        async for c in adapt._solve(node, 1, False):
            chunks.append(c)

    asyncio.run(_go())
    return chunks


def test_exit_loop_stops_parent_iteration():
    """P0-1 回归：子任务 ValidationStopEvent（exit_loop=True）后，父节点必须停止迭代。

    修复前（or 条件）：root 收到 exit_loop 后仍继续执行 executor（call#3/#4/... 无限）；
    修复后（and 条件）：root 仅执行 1 次 + sub 1 次 = 2 次后退出。
    """
    adapt, executor = _make_adapt(conf=0.5, sub_triggers_validation=True)
    root = _make_node("root-task")
    _collect(adapt, root)

    assert executor.calls == 2, (
        f"exit_loop 触发后父节点仍继续迭代（executor 被调用 {executor.calls} 次，应为 2）"
    )


def test_while_iteration_bounded_on_flat_expand():
    """P0-2 回归：supervisor 恒 conf=0.5（expand 区间）不收敛时，外层 while 必须有界。

    修复前：expand/refine 递归返回后无 break，attempts 不随迭代自增 → 无限迭代
    （实测 32+ 次 executor 调用仍不终止）；修复后：MAX_TASK_ATTEMPTS=3 约束迭代，
    任务以 failed:max_attempts 终止，executor 调用有界。
    """
    adapt, executor = _make_adapt(conf=0.5)
    root = _make_node("root-task")
    _collect(adapt, root)

    assert root.status == "failed:max_attempts", f"任务未按 max_attempts 终止，status={root.status}"
    assert executor.calls <= 10, (
        f"外层 while 迭代无界（executor 被调用 {executor.calls} 次，应 ≤10）"
    )


def test_while_iteration_bounded_on_refine():
    """P0-2 回归：supervisor 恒 conf=0.7（refine 区间）时，refine 递归 + while 迭代同样有界。

    refine 分支会替换 node（node = updated_tasks[0]，同文本共享 attempts）并递归，
    修复后最多 3 次真实执行即 failed:max_attempts 终止（node 被替换，故以产物断言）。
    """
    adapt, executor = _make_adapt(conf=0.7)
    root = _make_node("root-task")
    chunks = _collect(adapt, root)

    assert executor.calls == 3, (
        f"refine 路径执行次数异常（executor 被调用 {executor.calls} 次，应为 3）"
    )
    assert any(
        isinstance(c, str) and "max attempts" in c for c in chunks
    ), f"refine 路径未按 max_attempts 终止，产物={chunks[:5]}"
