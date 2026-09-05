"""TaskState：有界任务运行时状态，替代无界的 interaction_history（O(n²) → O(1)）。

切片 1 最小子集：recent_decisions（deque 有界）+ record_decision（字段裁剪）+ render_history。
后续切片扩展 index_view / current_surface / budget / pending_threats。

设计原则（对齐 AGENTS.md 简洁优先）：
- 每轮 record_decision 只存裁剪后的摘要字符串，不存全量 output / exec_log。
- recent_decisions 用 deque(maxlen=N)，append 自动淘汰最旧，第 i 轮 prompt 只携带最近 N 轮。
- render_history 输出 O(1) 大小，不随轮次增长——消灭 architecture.py interaction_history 的 O(n²) 累积。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from pobi_agent.utils.functions import num_tokens_from_string


@dataclass
class TaskState:
    """有界任务运行时状态。

    替代 architecture.py _solve 中无界的 interaction_history: list[str]。
    每次 _solve 调用新建一个实例（与原 interaction_history 作用域一致），
    递归子任务各自独立，不跨节点共享。
    """

    max_decisions: int = 5
    recent_decisions: deque = field(default_factory=lambda: deque(maxlen=5))
    _iteration: int = 0

    def __post_init__(self) -> None:
        # field(default_factory) 写死了 maxlen=5，需按 max_decisions 重建为有界 deque。
        self.recent_decisions = deque(maxlen=self.max_decisions)

    def record_decision(
        self,
        task: str,
        achieved: bool,
        confidence: float,
        summary: str,
        evidence: str = "",
    ) -> None:
        """记录一轮决策摘要（字段已裁剪，deque 自动有界淘汰）。

        与原 interaction_entry 的差异：
        - 去掉 Subagent calls（全量子 agent 调用日志，Summary 已覆盖 supervisor 对结果的总结）。
        - task / summary / evidence 均限长，避免单轮 entry 本身膨胀。
        """
        self._iteration += 1
        status = "achieved" if achieved else "not achieved"
        lines = [
            f"--- Iteration {self._iteration} ---",
            f"Task: {task[:120]}",
            f"Result: {status} | confidence={confidence:.2f}",
        ]
        if summary:
            lines.append(f"Summary: {summary[:500]}")
        if evidence:
            lines.append(f"Evidence: {evidence[:300]}")
        self.recent_decisions.append("\n".join(lines))

    def render_history(self) -> str:
        """渲染最近 N 轮决策摘要为 prompt 块（O(1) 大小，不随轮次增长）。

        无历史时返回空字符串，调用方据此判断是否注入 history_block。
        """
        if not self.recent_decisions:
            return ""
        return (
            "## Previous Supervisor Iterations (DO NOT repeat these actions)\n"
            + "\n".join(self.recent_decisions)
        )


def window_messages(
    history: list[dict],
    max_messages: int = 24,
    max_tokens: int = 6000,
) -> list[dict]:
    """对 supervisor 的 message_history（OpenAI 风格 list[dict]）做滚动窗口裁剪。

    裁剪边界在「我们拥有的驱动循环」(run() 之间)，版本无关、可回退：
    - 保留首条消息（连续性锚点），其余按条数 + token 双约束丢弃最旧的中间轮次；
    - 绝不丢弃落库事实（事实在 recon_facts / AVFS，不在此列表内）。
    - 注意：调用方须先剥离 CoreAgent 预置的 system 消息（raw_messages[1:]），
      本函数仅对对话轮次（含首条 user/assistant）做窗口化。

    history 为空或长度未超阈值时原样返回。
    """
    if not history:
        return []
    head = history[:1]  # 首条：连续性锚点，永裁
    rest = list(history[1:])
    budget_msgs = max(0, max_messages - len(head))
    if len(rest) > budget_msgs:
        rest = rest[-budget_msgs:]
    kept = head + rest
    if max_tokens and _est_tokens(kept) > max_tokens:
        while rest and _est_tokens(head + rest) > max_tokens:
            rest.pop(0)
        kept = head + rest
    return kept


def _est_tokens(messages: list[dict]) -> int:
    return sum(num_tokens_from_string(m.get("content") or "") for m in messages)
