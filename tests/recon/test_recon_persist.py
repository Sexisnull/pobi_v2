"""侦察产物落地单元测试。

验证 supervisor 调用 requester/shell 后，_persist_recon_facts 能把 agent 输出文本中
的端点 / 技术栈结构化写入本地 recon 库（recon_facts / recon_endpoints / recon_techniques），
且重复调用保持幂等（不重复插入）。
"""

from __future__ import annotations

import uuid
from pathlib import Path

from pobi_agent.agents.components import executor as executor_mod
from pobi_agent.agents.factory import AgentOutput
from pobi_agent.config.settings import ModelSpec
from pobi_agent.context.context_engine import ContextEngine
from pobi_agent.recon.store import ReconStore


def _make_engine(tmp_root: Path) -> ContextEngine:
    task_id = uuid.uuid4()
    recon_store = ReconStore.for_task(
        task_id=str(task_id),
        task_root=str(tmp_root),
    )
    engine = ContextEngine(
        model=ModelSpec(provider="test", model_name="test-model", api_key=None, base_url=None),
        session_id=task_id,
        recon_store=recon_store,
    )
    return engine


def _sample_output() -> AgentOutput:
    return AgentOutput(
        detailed_summary=(
            "Discovered endpoints: /login.php, /about.php, /api/users, "
            "/config/ and /dvwa/vulnerabilities/sqli/. The stack is PHP and "
            "Apache with MySQL backend. Login page requires auth."
        ),
        thoughts="Recon found DVWA 1.10 running on Apache/2.4.25 (Debian) with PHP/7.4.",
        confidence_score=0.7,
    )


def test_persist_creates_facts_and_endpoints(tmp_path: Path) -> None:
    engine = _make_engine(tmp_path)
    executor_mod._persist_recon_facts(engine, "requester", _sample_output())

    facts = engine.recon_store.list_facts(task_id=str(engine.session_id))
    categories = {f["category"] for f in facts}
    assert "endpoint" in categories
    assert "technology" in categories


def test_persist_endpoints_have_auth_marker(tmp_path: Path) -> None:
    engine = _make_engine(tmp_path)
    executor_mod._persist_recon_facts(engine, "requester", _sample_output())

    endpoints = engine.recon_store.list_endpoints(task_id=str(engine.session_id))
    auth_paths = {e["path"] for e in endpoints if e.get("auth_required")}
    assert "/login.php" in auth_paths


def test_persist_idempotent(tmp_path: Path) -> None:
    engine = _make_engine(tmp_path)
    out = _sample_output()
    executor_mod._persist_recon_facts(engine, "requester", out)
    executor_mod._persist_recon_facts(engine, "requester", out)  # 重复调用

    facts = engine.recon_store.list_facts(task_id=str(engine.session_id))
    endpoint_facts = [f for f in facts if f["category"] == "endpoint"]
    # 同一 endpoint 不应重复落库
    assert len(endpoint_facts) == len({f["key"] for f in endpoint_facts})


def test_persist_noop_when_store_missing() -> None:
    engine = ContextEngine(
        model=ModelSpec(provider="test", model_name="test-model", api_key=None, base_url=None),
        session_id=uuid.uuid4(),
        recon_store=None,
    )
    # 不应抛异常
    executor_mod._persist_recon_facts(engine, "requester", _sample_output())
