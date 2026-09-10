"""运行指令通道回归测试（人工干预机制，审计 P0 修复固化）。

覆盖四条关键路径：
1. key 一致性：写入用 task_id，消费必须用 session_id（== task_id）；
   原缺陷用 agent_id（本地独立 UUID）读取，恒为空（复现回归）。
2. 通道语义：drain 消费即清空；peek 只读不消费。
3. ContextEngine 注入可见性：operator instructions 进入 get_unified_context
   的最高优先级 section，supervisor 每轮重建时必然看到。
4. 上限/截断/重置：指令累积有界，reset 清空。

不依赖外部 LLM / 浏览器 / 数据库 / Redis。
"""
from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest

import pobi_v2.engine.instruction_channel as ic
from pobi_agent.context.context_engine import ContextEngine, StructuredContext


def _isolated_memory_store(monkeypatch):
    """把指令通道全局单例替换为独立的 memory store，避免污染其他测试。"""
    store = ic._MemoryInstructionStore()
    monkeypatch.setattr(ic, "_store", store)
    return store


# ---------- key 一致性（审计问题 1 回归） ----------

def test_instruction_key_mismatch_regression(monkeypatch):
    """写入 key=task_id；读取 key=session_id(==task_id) 必须拿到，agent_id 必须为空。

    复现原缺陷：DeadEndAgent 消费端用 self.agent_id（本地持久化独立 UUID），
    与 routers/instruction.py 写入的 str(task.id) 恒不相等 → 干预 100% 失效。
    """
    _isolated_memory_store(monkeypatch)
    task_id = uuid4()          # == DeadEndAgent.session_id
    agent_id = uuid4()         # 模拟 DeadEndAgent.agent_id（独立 UUID）

    async def _go():
        await ic.queue_instruction(str(task_id), "只测试 SQL 注入，忽略其他")
        # 修复后：drain_instructions(self.session_id) 必须拿到
        got = await ic.drain_instructions(task_id)
        assert [p.instruction for p in got] == ["只测试 SQL 注入，忽略其他"]

        # 修复前路径：drain_instructions(self.agent_id) 必须为空
        await ic.queue_instruction(str(task_id), "第二条指令")
        assert await ic.drain_instructions(agent_id) == []
        # 且指令仍在原 key 下，未被 agent_id 误消费
        got2 = await ic.drain_instructions(task_id)
        assert [p.instruction for p in got2] == ["第二条指令"]

    asyncio.run(_go())


# ---------- 通道语义：drain 消费 / peek 只读 ----------

def test_drain_consumes_and_peek_preserves(monkeypatch):
    _isolated_memory_store(monkeypatch)
    task_id = uuid4()

    async def _go():
        await ic.queue_instruction(str(task_id), "指令A")
        await ic.queue_instruction(str(task_id), "指令B")
        # peek 不消费
        peeked = await ic.peek_instructions(task_id)
        assert [p.instruction for p in peeked] == ["指令A", "指令B"]
        # 仍在队列
        remain = await ic.peek_instructions(task_id)
        assert len(remain) == 2
        # drain 消费即清空
        drained = await ic.drain_instructions(task_id)
        assert [p.instruction for p in drained] == ["指令A", "指令B"]
        assert await ic.drain_instructions(task_id) == []
        assert await ic.peek_instructions(task_id) == []

    asyncio.run(_go())


# ---------- ContextEngine 注入可见性（审计问题 2 回归） ----------

def test_operator_instruction_appears_in_unified_context():
    """指令注入后必须出现在 get_unified_context（supervisor 每轮重建的上下文）。"""
    sc = StructuredContext(target="https://example.com", goal="FLAG{xxx}")
    sc.add_operator_instruction("只测试 SQL 注入，忽略其他")
    unified = sc.get_unified_context()
    assert "OPERATOR INSTRUCTIONS" in unified
    assert "只测试 SQL 注入，忽略其他" in unified
    # 指令 section 是首个 '##' 开头的 section（紧跟目标之后，最高优先级）
    first_h2 = unified.find("## ")
    assert first_h2 == unified.find("## OPERATOR INSTRUCTIONS")


def test_operator_instruction_isolation_from_recon():
    """指令不写入 RECON 库、不混入 facts（与 add_discovered_fact 隔离）。"""
    sc = StructuredContext()
    sc.add_operator_instruction("只测试 SQL 注入，忽略其他")
    assert sc.get_operator_instructions() == ["只测试 SQL 注入，忽略其他"]
    assert not sc.facts  # 指令不产生 DiscoveredFact


def test_operator_instruction_cap_and_truncate():
    sc = StructuredContext()
    for i in range(20):
        sc.add_operator_instruction(f"指令-{i}")
    ops = sc.get_operator_instructions()
    assert len(ops) <= 8
    assert ops[-1] == "指令-19"      # 保留最近的
    assert "指令-0" not in ops       # 最早的被裁剪

    # 长指令截断
    sc2 = StructuredContext()
    long_ins = "很" * 1000
    sc2.add_operator_instruction(long_ins)
    assert len(sc2.get_operator_instructions()[0]) <= 300 + len("…[截断]")
    assert sc2.get_operator_instructions()[0].endswith("…[截断]")


def test_operator_instruction_reset_clears():
    sc = StructuredContext()
    sc.add_operator_instruction("某指令")
    sc.reset()
    assert sc.get_operator_instructions() == []
    # 重置后仍可注入
    sc.add_operator_instruction("新指令")
    assert "新指令" in sc.get_unified_context()


def test_context_engine_delegates_operator_instruction():
    """ContextEngine.add_operator_instruction 委托到 structured 且被 unified 呈现。

    用 object.__new__ 绕过 __init__（避免在用户主目录创建 run_context 目录），
    仅验证委托与渲染路径。
    """
    ce = object.__new__(ContextEngine)
    ce.structured = StructuredContext(target="https://example.com")
    ce.target = "https://example.com"
    ce.recon_store = None
    ce.add_operator_instruction("只测试 SQL 注入，忽略其他")
    assert ce.get_operator_instructions() == ["只测试 SQL 注入，忽略其他"]
    assert "只测试 SQL 注入，忽略其他" in ce.get_unified_context()
    assert ce.structured.facts == {}  # 未污染 facts


# ---------- 空值/异常路径 ----------

def test_empty_instruction_ignored():
    sc = StructuredContext()
    sc.add_operator_instruction("")
    sc.add_operator_instruction("   ")
    assert sc.get_operator_instructions() == []


def test_drain_unknown_task_returns_empty(monkeypatch):
    _isolated_memory_store(monkeypatch)

    async def _go():
        assert await ic.drain_instructions(uuid4()) == []
        assert await ic.peek_instructions(uuid4()) == []

    asyncio.run(_go())
