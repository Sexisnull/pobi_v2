"""Sitemap（katana）引擎与 requester 对齐测试。

覆盖：
1. parse_katana_jsonl：字段映射 + 坏行容错
2. should_keep：静态资源 / 噪音路径 / 分页参数变体过滤，真实端点保留
3. persist_sitemap：事务 + 端点 + 足迹落库；auth_required 判定；blob 外置
4. build_katana_command：Cookie / header 认证注入 + -ef 静态过滤 + shlex 引用防注入
5. run_sitemap：katana 未装 → skipped 不阻断；装 → 解析落库 completed
6. requester 对齐：_persist_http_tx 结构化写入 recon_http_transactions
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from pobi_agent.recon import ReconStore
from pobi_agent.tools.browser_automation import _persist_http_tx
from pobi_agent.tools.sitemap import (
    KatanaEntry,
    parse_katana_jsonl,
    persist_sitemap,
    should_keep,
)
from pobi_v2.engine.sitemap.katana_runner import build_katana_command

# katana JSONL 样例（-j 输出结构：request.method/endpoint/headers/body + response.status_code/...）
SAMPLE_LINES = [
    json.dumps(
        {
            "timestamp": "2026-09-02T10:00:00+08:00",
            "request": {
                "method": "GET",
                "endpoint": "http://ex.com/",
                "raw": "GET / HTTP/1.1\r\nHost: ex.com\r\n",
                "headers": {"User-Agent": "katana"},
                "body": "",
            },
            "response": {
                "status_code": 200,
                "headers": {"Content-Type": "text/html"},
                "body": "<html><title>Home</title></html>",
                "technologies": ["React"],
                "raw": "HTTP/1.1 200 OK\r\n\r\n...",
            },
        },
        ensure_ascii=False,
    ),
    json.dumps(
        {
            "timestamp": "2026-09-02T10:00:01+08:00",
            "request": {
                "method": "GET",
                "endpoint": "http://ex.com/admin/login",
                "headers": {},
                "body": "",
            },
            "response": {
                "status_code": 302,
                "headers": {"Location": "/login"},
                "body": "",
                "technologies": [],
            },
        },
        ensure_ascii=False,
    ),
    # 静态资源（应过滤）
    json.dumps(
        {
            "request": {"method": "GET", "endpoint": "http://ex.com/static/app.js", "headers": {}, "body": ""},
            "response": {"status_code": 200, "headers": {}, "body": "x", "technologies": []},
        },
        ensure_ascii=False,
    ),
    # 分页参数变体（应过滤）
    json.dumps(
        {
            "request": {"method": "GET", "endpoint": "http://ex.com/list?page=2&sort=asc", "headers": {}, "body": ""},
            "response": {"status_code": 200, "headers": {}, "body": "x", "technologies": []},
        },
        ensure_ascii=False,
    ),
    # 404 噪音路径（应过滤）
    json.dumps(
        {
            "request": {"method": "GET", "endpoint": "http://ex.com/404", "headers": {}, "body": ""},
            "response": {"status_code": 404, "headers": {}, "body": "x", "technologies": []},
        },
        ensure_ascii=False,
    ),
    # 坏行（应跳过）
    "this is not json {{{",
    "",
]


def test_parse_katana_jsonl():
    entries = parse_katana_jsonl("\n".join(SAMPLE_LINES))
    assert len(entries) == 5  # 6 行有效 JSON 中 4 条保留 + 2 条（静态/分页）仍解析（过滤在 should_keep）
    assert entries[0].method == "GET"
    assert entries[0].status_code == 200
    assert entries[0].technologies == ["React"]
    assert entries[1].status_code == 302
    # 坏行被跳过：有效 JSON 行数为 5
    assert len([e for e in entries if e.url]) == 5


def test_should_keep():
    assert not should_keep("http://ex.com/static/app.js")
    assert not should_keep("http://ex.com/wp-content/themes/t/style.css")
    assert not should_keep("http://ex.com/img/logo.png")
    assert not should_keep("http://ex.com/list?page=2&sort=asc")
    assert not should_keep("http://ex.com/404")
    assert should_keep("http://ex.com/")
    assert should_keep("http://ex.com/admin/login")
    assert should_keep("http://ex.com/api/users?id=1")  # 语义参数保留


def test_persist_sitemap(tmp_path: Path):
    store = ReconStore.for_task("task-s", str(tmp_path))
    try:
        entries = parse_katana_jsonl("\n".join(SAMPLE_LINES))
        stats = persist_sitemap(store, "task-s", entries, host_hint="ex.com")
        assert stats["total"] == 5
        assert stats["dropped"] == 3  # js / 分页 / 404
        assert stats["kept"] == 2  # / 与 /admin/login
        assert stats["transactions"] == 2
        assert stats["auth_required"] == 1  # 302→/login

        # 事务表
        txs = store.list_http_transactions("task-s")
        assert len(txs) == 2
        by_path = {t["path_normalized"]: t for t in txs}
        login = by_path["/admin/login"]
        assert login["status_code"] == 302
        assert login["auth_required"] is True
        assert login["source"] == "sitemap:katana"
        home = by_path["/"]
        assert home["response_title"] == "Home"
        assert home["tech_stack"] == ["React"]

        # 端点树由事务派生（单一真源），不再双写
        asyncio.run(store.derive_endpoints_from_transactions("task-s"))
        eps = store.list_endpoints("task-s")
        assert {e["path"] for e in eps} == {"/", "/admin/login"}
        assert all(e["discovered_via"] == "sitemap:katana" for e in eps)

        # 足迹防重
        techs = store.list_techniques("task-s")
        assert any(t["name"] == "sitemap|ex.com" for t in techs)
    finally:
        store.close()


def test_persist_sitemap_tiered_storage(tmp_path: Path):
    """响应体分层存储：<100KB full / 100KB-1MB compressed / >1MB digest。"""
    store = ReconStore.for_task("task-b", str(tmp_path))
    try:
        big = "<html><body>" + "A" * 150_000 + "</body></html>"  # 150KB → compressed
        entry = KatanaEntry(
            url="http://ex.com/big",
            method="GET",
            status_code=200,
            response_body=big,
            response_headers={"Content-Type": "text/html"},
        )
        persist_sitemap(store, "task-b", [entry], host_hint="ex.com")
        txs = store.list_http_transactions("task-b")
        assert len(txs) == 1
        tx = txs[0]
        assert tx["response_size"] == len(big)
        assert tx["storage_strategy"] == "compressed"
        assert tx["body_available"] is True
        assert len(tx["response_body"]) <= 200  # 列表仅 200 字符预览
        # 按需取全文：解压还原
        assert store.get_transaction_body("task-b", tx["id"]) == big
    finally:
        store.close()


def test_tiered_storage_digest_and_api(tmp_path: Path):
    """>1MB 非 API → digest（仅摘要）；json/xml 大响应 → compressed（全量保留）。"""
    store = ReconStore.for_task("task-d", str(tmp_path))
    try:
        huge = "<html>" + "B" * (2 * 1024 * 1024) + "</html>"
        store.insert_http_transaction(
            "task-d", url="http://ex.com/huge", path_normalized="/huge",
            method="GET", status_code=200, content_type="text/html", response_body=huge,
        )
        big_json = '{"data":[' + ",".join(f'"{i}"' for i in range(30000)) + "]}"
        store.insert_http_transaction(
            "task-d", url="http://ex.com/api", path_normalized="/api",
            method="GET", status_code=200, content_type="application/json",
            response_body=big_json,
        )
        txs = {t["url"]: t for t in store.list_http_transactions("task-d")}
        # digest：只存 2048 摘要，无全量
        assert txs["http://ex.com/huge"]["storage_strategy"] == "digest"
        assert txs["http://ex.com/huge"]["body_available"] is False
        assert len(txs["http://ex.com/huge"]["response_body"]) == 2048
        assert store.get_transaction_body("task-d", txs["http://ex.com/huge"]["id"]) == huge[:2048]
        # API json：大也全量（compressed 压缩保留，可解压还原）
        assert txs["http://ex.com/api"]["storage_strategy"] == "compressed"
        assert store.get_transaction_body("task-d", txs["http://ex.com/api"]["id"]) == big_json
    finally:
        store.close()


def test_upsert_to_pg_pg_unreachable_keeps_dirty(tmp_path):
    """PG 不可达：异常上抛（由任务出口容错），本地脏行不误打标 → 下次可重试。

    验证增量同步的安全语义：只有 PG 提交成功后本地才置 pg_synced_at。
    """
    import asyncio

    import pytest

    store = ReconStore.for_task("task-p", str(tmp_path))
    try:
        store.insert_http_transaction(
            "task-p", url="http://ex.com/a", path_normalized="/a",
            method="GET", status_code=200, content_type="text/html",
            response_body="<html>ok</html>",
        )

        class BrokenPG:
            async def __aenter__(self):
                raise RuntimeError("PG 不可达（本地测试）")

            async def __aexit__(self, *a):
                return False

        with pytest.raises(RuntimeError):
            asyncio.run(
                store.upsert_to_pg(
                    "00000000-0000-0000-0000-000000000001",
                    "00000000-0000-0000-0000-000000000002",
                    "task-p", lambda: BrokenPG(),
                )
            )
        # 未打标：本地事务仍是脏行（pg_synced_at IS NULL），下次 upsert 会重试。
        with store._session_factory() as s:
            from sqlalchemy import select as _sel
            from pobi_agent.recon.sqlite_models import ReconHttpTransaction as _T

            row = s.execute(_sel(_T).where(_T.task_id == "task-p")).scalar_one()
            assert row.pg_synced_at is None
    finally:
        store.close()


def test_build_katana_command_auth_and_sanitize():
    cmd = build_katana_command(
        "http://ex.com",
        cookies={"PHPSESSID": "abc123"},
        extra_headers={"Authorization": "Bearer tok"},
    )
    assert cmd.startswith("katana -u http://ex.com")
    assert "-j" in cmd and "-silent" in cmd and "-duc" in cmd
    assert "-ef" in cmd and "png,jpg" in cmd
    assert "-iqp" in cmd
    assert "-H 'Cookie: PHPSESSID=abc123'" in cmd
    assert "-H 'Authorization: Bearer tok'" in cmd
    # 无认证时不注入 Cookie
    assert "Cookie:" not in build_katana_command("http://ex.com")
    # target 含空格/引号时 shlex 引用防注入
    evil = build_katana_command("http://ex.com; rm -rf /")
    assert "rm" not in evil.split("-u")[1].split()[0]


def _fake_hooks():
    return SimpleNamespace(
        emit_phase_changed=lambda *a, **k: None,
        emit_tool_call_start=lambda *a, **k: None,
        emit_tool_call_end=lambda *a, **k: None,
    )


def test_run_sitemap_skipped_when_katana_missing(tmp_path: Path, monkeypatch):
    """katana 未安装 → status=skipped，不阻断任务。"""
    import pobi_v2.engine.sitemap.katana_runner as kr

    monkeypatch.setattr(kr, "katana_available", lambda: asyncio.sleep(0) or False)
    res = asyncio.run(
        kr.run_sitemap(
            task_id="task-x",
            target_url="http://ex.com",
            task_root=tmp_path,
            hooks=_fake_hooks(),
        )
    )
    assert res["status"] == "skipped"
    assert "katana" in res.get("error", "")


def test_run_sitemap_completed_and_persisted(tmp_path: Path, monkeypatch):
    """katana 已装 → 执行 → 解析 → 落库 completed。"""
    import pobi_v2.engine.sitemap.katana_runner as kr

    async def fake_available():
        return True

    async def fake_run(target, **kw):
        return {"ok": True, "stdout": "\n".join(SAMPLE_LINES), "stderr": "", "exit_code": 0}

    monkeypatch.setattr(kr, "katana_available", fake_available)
    monkeypatch.setattr(kr, "run_katana", fake_run)
    res = asyncio.run(
        kr.run_sitemap(
            task_id="task-y",
            target_url="http://ex.com",
            task_root=tmp_path,
            hooks=_fake_hooks(),
        )
    )
    assert res["status"] == "completed"
    assert res["summary"]["kept"] == 2
    assert res["summary"]["dropped"] == 3
    # 端点树不再在 sitemap 内双写，改由事务派生
    assert "endpoints" not in res["summary"]
    # 原始 JSONL 落盘（防误过滤复盘）
    raw_path = tmp_path / "sitemap" / "katana_raw.jsonl"
    assert raw_path.exists()
    assert raw_path.read_text(encoding="utf-8").count("\n") >= 5


def test_requester_tx_alignment(tmp_path: Path):
    """requester 请求结构化写入 recon_http_transactions（与 sitemap 同 schema）。"""
    store = ReconStore.for_task("task-r", str(tmp_path))
    try:
        store.ensure_session("task-r")

        def fake_add(**kw):
            store.insert_http_transaction("task-r", **kw)

        ctx = SimpleNamespace(
            deps=SimpleNamespace(context=SimpleNamespace(add_recon_http_transaction=fake_add))
        )
        raw_req = (
            "POST /admin/login HTTP/1.1\r\n"
            "Host: ex.com\r\n"
            "Content-Type: application/x-www-form-urlencoded\r\n"
            "\r\n"
            "user=admin&pass=1"
        )
        resp = (
            "HTTP/1.1 200 OK\r\nContent-Type: text/html\r\n\r\n"
            "<html><title>Admin</title></html>"
        ).encode()
        _persist_http_tx(ctx, raw_req, False, True, [resp])

        txs = store.list_http_transactions("task-r")
        assert len(txs) == 1
        tx = txs[0]
        assert tx["source"] == "agent:requester"
        assert tx["path_normalized"] == "/admin/login"
        assert tx["method"] == "POST"
        assert tx["status_code"] == 200
        assert tx["request_body"] == "user=admin&pass=1"
        assert tx["auth_used"] is True
        assert tx["response_title"] == "Admin"
        assert tx["content_type"] == "text/html"
    finally:
        store.close()


def test_requester_tx_skips_error_text(tmp_path: Path):
    """请求失败（非 HTTP 响应）不记事务。"""
    store = ReconStore.for_task("task-e", str(tmp_path))
    try:
        store.ensure_session("task-e")

        def fake_add(**kw):
            store.insert_http_transaction("task-e", **kw)

        ctx = SimpleNamespace(
            deps=SimpleNamespace(context=SimpleNamespace(add_recon_http_transaction=fake_add))
        )
        _persist_http_tx(
            ctx,
            "GET /foo HTTP/1.1\r\nHost: ex.com\r\n",
            False,
            False,
            ["Request failed: connection reset".encode()],
        )
        assert store.list_http_transactions("task-e") == []
    finally:
        store.close()
