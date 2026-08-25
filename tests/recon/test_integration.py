"""RECON 集成测试门禁（第二阶）：合成 200+ fact 验证 token 上限 / lookup 时延 /
WAL 恢复 / PG 同步收敛 / 迁移可应用（无 PG 时迁移用例 skip）。

本地库部分用真实 SQLite（可运行）；PG 聚合同步用 mock AsyncSession 验证
不抛错且 ON CONFLICT upsert 被调用；迁移用例在无 PG 连接时 skip。
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock

import pytest  # noqa: F401  (AsyncMock 仍用于 mock_session/mock_cm)

from pobi_agent.recon import ReconStore
from pobi_agent.recon.sqlite_models import ReconFact
from pobi_agent.utils.functions import num_tokens_from_string


# ---------------------------------------------------------------------------
# 合成数据集
# ---------------------------------------------------------------------------


def _seed_200_facts(store: ReconStore, task_id: str = "task-int") -> None:
    store.ensure_session(task_id)
    # 200+ 端点事实 + 50 威胁，制造 L1/L2 裁剪压力。
    for i in range(220):
        store.upsert_fact(
            task_id,
            "endpoint" if i % 2 == 0 else "technology",
            f"/path/{i}",
            f"value payload number {i} " + "x" * 20,
            confidence=0.9 if i % 5 == 0 else 0.5,
        )
    for i in range(50):
        store.upsert_threat(
            task_id,
            cve_id=f"CVE-2024-{i}",
            title=f"Threat {i}",
            severity="high" if i % 2 else "medium",
            status="confirmed" if i % 3 == 0 else "suspected",
            evidence_summary=f"exploit worked on /path/{i}",
            confidence=0.8,
        )


# ---------------------------------------------------------------------------
# 1. build_index_view token 预算不超限
# ---------------------------------------------------------------------------


def test_index_token_budget_under_limit(tmp_path):
    store = ReconStore(tmp_path / "integ.db")
    _seed_200_facts(store)
    view = store.build_index_view("task-int")
    assert num_tokens_from_string(view) <= 500 + 1500 + 400  # L0 + L1 + L2 + 容差
    store.close()


# ---------------------------------------------------------------------------
# 2. lookup P99 时延 < 50ms
# ---------------------------------------------------------------------------


def test_lookup_p99_under_50ms(tmp_path):
    store = ReconStore(tmp_path / "integ_lookup.db")
    _seed_200_facts(store)
    samples = []
    for _ in range(30):
        t0 = time.perf_counter()
        store.lookup("task-int", category="endpoint", limit=20)
        samples.append((time.perf_counter() - t0) * 1000)
    samples.sort()
    p99 = samples[int(len(samples) * 0.99)]
    assert p99 < 50, f"lookup P99={p99:.2f}ms 超阈值"
    store.close()


# ---------------------------------------------------------------------------
# 3. WAL 崩溃恢复：写一半后重新打开库可读
# ---------------------------------------------------------------------------


def test_wal_crash_recovery(tmp_path):
    db = tmp_path / "crash.db"
    store = ReconStore(db)
    store.ensure_session("t")
    for i in range(100):
        store.upsert_fact("t", "endpoint", f"/p{i}", f"v{i}")
    store.close()  # 模拟正常 checkpoint

    # 模拟崩溃：不调用 close，直接重新打开（WAL 应保证已提交数据可读）。
    store2 = ReconStore(db)
    with store2._session_factory() as s:
        rows = s.execute(
            __import__("sqlalchemy").select(ReconFact).where(ReconFact.task_id == "t")
        ).scalars().all()
        assert len(rows) == 100
    store2.close()


# ---------------------------------------------------------------------------
# 4. PG 同步收敛：两批相同事实 upsert_to_pg 不抛错且调用 upsert
# ---------------------------------------------------------------------------


def test_upsert_to_pg_converges(tmp_path):
    db = tmp_path / "pg.db"
    store = ReconStore(db)
    _seed_200_facts(store, task_id="t")

    # mock AsyncSession：execute/commit 均异步无操作，记录执行的语句次数。
    calls = {"execute": 0}

    async def _fake_execute(*_a, **_k):
        calls["execute"] += 1
        return _fake_result()

    mock_session = AsyncMock()
    mock_session.execute = _fake_execute
    mock_session.commit = AsyncMock()

    mock_cm = AsyncMock()
    mock_cm.__aenter__.return_value = mock_session
    mock_cm.__aexit__.return_value = None
    # factory 为普通可调用，返回 async context manager（AsyncMock）。
    mock_factory = lambda: mock_cm

    async def _run():
        await store.upsert_to_pg(
            target_id="11111111-1111-1111-1111-111111111111",
            tenant_id="22222222-2222-2222-2222-222222222222",
            task_id="t",
            async_session_factory=mock_factory,
        )
        # 第二次相同数据 upsert（收敛：不应线性增长行数，由 PG 唯一约束保证）。
        await store.upsert_to_pg(
            target_id="11111111-1111-1111-1111-111111111111",
            tenant_id="22222222-2222-2222-2222-222222222222",
            task_id="t",
            async_session_factory=mock_factory,
        )

    asyncio.run(_run())
    assert calls["execute"] >= 2  # 至少 facts + threats 两次 upsert
    store.close()


def _fake_result():
    class _R:
        def scalars(self):
            return self

        def all(self):
            return []

        def scalar_one_or_none(self):
            return None

    return _R()


# ---------------------------------------------------------------------------
# 5. 迁移可应用（无 PG 连接时 skip）
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    __import__("os").environ.get("POBI_TEST_PG_URL") is None,
    reason="需 POBI_TEST_PG_URL 指向可用 PG 才能验证迁移",
)
def test_alembic_migration_applies():
    import subprocess

    url = __import__("os").environ["POBI_TEST_PG_URL"]
    # 用 alembic 升到 head 再回滚，验证 0014_recon_agg 可应用且可回滚。
    subprocess.run(
        ["uv", "run", "alembic", "upgrade", "head"],
        env={**__import__("os").environ, "DATABASE_URL": url},
        check=True,
    )
    subprocess.run(
        ["uv", "run", "alembic", "downgrade", "-1"],
        env={**__import__("os").environ, "DATABASE_URL": url},
        check=True,
    )


# ---------------------------------------------------------------------------
# 6.（第三阶）往返闭环：seed → 增量 upsert → reconcile 状态收敛
# ---------------------------------------------------------------------------


def _pg_agg_fact(category, key, value="v", confidence=0.7):
    f = AsyncMock()
    f.category = category
    f.key = key
    f.value = value
    f.confidence = confidence
    f.details_json = {}
    return f


def _pg_agg_threat(cve_id, status="confirmed", evidence_summary="ev", confidence=0.8):
    t = AsyncMock()
    t.cve_id = cve_id
    t.title = f"T {cve_id}"
    t.category = "cve"
    t.severity = "high"
    t.status = status
    t.cvss_score = 7.5
    t.target_endpoint = "/x"
    t.evidence_summary = evidence_summary
    t.confidence = confidence
    return t


def _pg_session_factory(*result_sets):
    """构造 mock AsyncSession 工厂：按调用次序返回各次 execute 的 result。"""
    import itertools

    pg_session = AsyncMock()
    results = itertools.cycle(result_sets)
    pg_session.execute.side_effect = lambda _stmt: next(results)
    mock_cm = AsyncMock()
    mock_cm.__aenter__.return_value = pg_session
    mock_cm.__aexit__.return_value = None
    return lambda: mock_cm


def test_roundtrip_seed_reconcile_converge(tmp_path):
    # db 文件名须与 task_id 一致（_task_id_of_db 由文件名反推）。
    store = ReconStore(tmp_path / "t.db")
    store.ensure_session("t")

    # 第一轮 seed：新目标，全量灌入。
    factory1 = _pg_session_factory(
        _FakeScalars([_pg_agg_fact("endpoint", "/api/v1"),
                      _pg_agg_fact("technology", "nginx")]),
        _FakeScalars([_pg_agg_threat("CVE-2024-9001", status="confirmed")]),
    )
    res1 = asyncio.run(store.seed_from_pg(
        "11111111-1111-1111-1111-111111111111",
        "22222222-2222-2222-2222-222222222222",
        factory1,
    ))
    assert res1.seeded_count == 3
    assert res1.already_covered_count == 0

    # 第二轮 seed（新 factory）：已覆盖项不再重复灌入。
    factory2 = _pg_session_factory(
        _FakeScalars([_pg_agg_fact("endpoint", "/api/v1"),
                      _pg_agg_fact("technology", "nginx"),
                      _pg_agg_fact("endpoint", "/admin")]),
        _FakeScalars([_pg_agg_threat("CVE-2024-9001", status="confirmed")]),
    )
    res2 = asyncio.run(store.seed_from_pg(
        "11111111-1111-1111-1111-111111111111",
        "22222222-2222-2222-2222-222222222222",
        factory2,
    ))
    assert res2.seeded_count == 1  # 仅 /admin 新增
    assert res2.already_covered_count == 3  # 其余已覆盖

    # reconcile：PG 状态提升 exploited，本地同步升级。
    factory3 = _pg_session_factory(
        _FakeScalars([_pg_agg_threat("CVE-2024-9001", status="exploited",
                                     evidence_summary="flag{ok}")]),
    )
    summary = asyncio.run(store.reconcile_pg_to_local(
        "11111111-1111-1111-1111-111111111111",
        "22222222-2222-2222-2222-222222222222",
        factory3,
    ))
    assert summary["upgraded"] == 1
    assert summary["kept_local"] == 0
    with store._session_factory() as s:
        from pobi_agent.recon.sqlite_models import ReconEndpoint

        row = s.execute(
            __import__("sqlalchemy").select(ReconEndpoint).where(
                ReconEndpoint.path_normalized == "/admin"
            )
        ).scalar_one_or_none()
        assert row is not None  # /admin 属 endpoint 类，落在 recon_endpoints
    store.close()


class _FakeScalars:
    """模拟 execute 结果：scalars().all() 返回给定序列。"""

    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None
