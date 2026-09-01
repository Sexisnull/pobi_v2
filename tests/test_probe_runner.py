"""链路探针（probe）本地目录行为测试。

探针是一次性连通探测（Kali 内 curl + 一次 LLM 解读），不产出需留存的本地产物，
故运行结束必须清理 tasks/<task_id>/，避免每次探测在磁盘留下空目录。

验证：
- run_probe_agent 正常结束后任务目录被移除（走真实调用路径，仅 mock 执行体）
- 目录非空时保留并记 warning（避免静默丢弃意外写入）
- 执行体抛异常时目录同样被清理
- 目录不存在时清理为 no-op
"""
from __future__ import annotations

import asyncio
import logging
from uuid import uuid4

import pytest

import pobi_agent.constants as constants
from pobi_v2.engine import probe_runner as pr


@pytest.fixture
def probe_task_root(tmp_path, monkeypatch):
    """把 TASKS_ROOT 重定向到临时目录，返回该临时根。

    run_probe_agent 在执行时从 pobi_agent.constants 取 TASKS_ROOT，
    故 patch 模块属性即可让目录落在 tmp_path 下。
    """
    monkeypatch.setattr(constants, "TASKS_ROOT", tmp_path)
    return tmp_path


def _stub_body(monkeypatch, exc: Exception | None = None) -> None:
    """替换探针执行体，绕开真实 Kali / LLM 调用。"""
    if exc is not None:

        async def _boom(**kwargs):
            raise exc

        monkeypatch.setattr(pr, "_run_probe_body", _boom)
        return

    async def _ok(**kwargs):
        return {"summary": "目标可达", "confidence": 0.9, "findings": []}

    monkeypatch.setattr(pr, "_run_probe_body", _ok)


def test_probe_removes_task_dir_after_success(probe_task_root, monkeypatch):
    """正常结束后不应在 tasks/ 下留下任务目录。"""
    _stub_body(monkeypatch)
    task_id = uuid4()

    async def _run():
        return await pr.run_probe_agent(task=None, target=None, task_id=task_id)

    outcome = asyncio.run(_run())

    assert outcome["summary"] == "目标可达"
    assert not (probe_task_root / str(task_id)).exists()


def test_probe_removes_task_dir_after_failure(probe_task_root, monkeypatch):
    """执行体抛异常时（如 Kali 不可达）同样清理，避免失败探测留孤儿。"""
    _stub_body(monkeypatch, exc=RuntimeError("Kali 不可达"))
    task_id = uuid4()

    async def _run():
        return await pr.run_probe_agent(task=None, target=None, task_id=task_id)

    with pytest.raises(RuntimeError):
        asyncio.run(_run())

    assert not (probe_task_root / str(task_id)).exists()


def test_probe_keeps_nonempty_task_dir(probe_task_root, caplog):
    """目录非空说明有意料之外的写入，应保留并告警，不得静默删除。"""
    task_id = uuid4()
    task_root = probe_task_root / str(task_id)
    task_root.mkdir(parents=True)
    (task_root / "metrics.json").write_text("{}", encoding="utf-8")

    with caplog.at_level(logging.WARNING):
        pr._cleanup_probe_task_root(task_root, task_id)

    assert task_root.exists()
    assert "非空" in caplog.text


def test_probe_cleanup_missing_dir_is_noop(probe_task_root):
    """目录不存在时不应抛异常。"""
    pr._cleanup_probe_task_root(probe_task_root / "does-not-exist", uuid4())
