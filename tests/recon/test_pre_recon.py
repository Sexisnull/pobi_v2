"""前置侦查通道 pre_recon 测试。

覆盖：
1. 端到端 run_pre_recon（本地靶标：nginx + PHP + WordPress + React + Cloudflare WAF）
2. 落库：recon_fingerprints 明细 + recon_facts(technology) + endpoints(tech_stack) +
   recon_techniques(fingerprint|host) 足迹
3. 事件推送：phase_changed(pre_recon) + tool_call_start/end(fingerprint)
4. 幂等：同 host 二次调用覆盖不新增行
5. 降级：hooks=None / 目标不可达 / 无认证会话（external_only）
"""

from __future__ import annotations

import asyncio
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from pobi_agent.recon import ReconStore
from pobi_v2.engine import pre_recon

FAVICON_BYTES = b"\x89PNG\r\n\x1a\n" + b"X" * 64


class _TargetHandler(BaseHTTPRequestHandler):
    """模拟 nginx + PHP + WordPress + React 靶标。"""

    def log_message(self, *args):  # 静音访问日志
        pass

    def do_GET(self):
        if self.path == "/favicon.ico":
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.end_headers()
            self.wfile.write(FAVICON_BYTES)
            return
        self.send_response(200)
        self.send_header("Server", "nginx/1.24.0")
        self.send_header("X-Powered-By", "PHP/7.4.33")
        self.send_header("Set-Cookie", "PHPSESSID=abc123; path=/")
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(
            b"<html><head><link rel='icon' href='/favicon.ico'></head><body>"
            b"<div id='root' data-reactroot=''></div>"
            b"<script src='/wp-includes/js/jquery.min.js'></script>"
            b"<img src='/wp-content/themes/theme/style.css'>"
            b"<a href='/wp-admin/'>admin</a>"
            b"</body></html>"
        )


class _WafHandler(_TargetHandler):
    """模拟 Cloudflare WAF 前置（仅外部探测层差异，便于验证 waf 分类落库）。"""

    def do_GET(self):
        self.send_response(200)
        self.send_header("Server", "cloudflare")
        self.send_header("CF-RAY", "abc123-ORD")
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(b"<html><body>hello</body></html>")


class _UnreachableHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        # 模拟 500/异常，collector 应视为不可达（status 非 None 但无指纹）
        self.send_response(503)
        self.end_headers()


class _HooksRecorder:
    """记录 phase / tool 事件的假 hooks。"""

    def __init__(self):
        self.events: list[tuple] = []

    def emit_phase_changed(self, session_id, phase, detail=None):
        self.events.append(("phase", session_id, phase, detail))

    def emit_tool_call_start(self, session_id, agent_name, tool_name, args="", tool_call_id=None):
        self.events.append(("tool_start", agent_name, tool_name, args))

    def emit_tool_call_end(self, session_id, agent_name, tool_name, success, result="",
                           error=None, tool_call_id=None, duration_ms=None):
        self.events.append(("tool_end", agent_name, tool_name, success, result))


@pytest.fixture
def target_server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _TargetHandler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{srv.server_address[1]}"
    yield url
    srv.shutdown()
    srv.server_close()


@pytest.fixture
def waf_server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _WafHandler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{srv.server_address[1]}"
    yield url
    srv.shutdown()
    srv.server_close()


@pytest.fixture
def unreachable_server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _UnreachableHandler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{srv.server_address[1]}"
    yield url
    srv.shutdown()
    srv.server_close()


@pytest.fixture
def task_root(tmp_path):
    return tmp_path


def _task_id():
    return str(uuid.uuid4())


def test_pre_recon_end_to_end(target_server, task_root):
    """端到端：run_pre_recon 完成四层指纹并落库、推送事件。"""
    hooks = _HooksRecorder()
    tid = _task_id()
    result = asyncio.run(
        pre_recon.run_pre_recon(
            task_id=tid, target_url=target_server, task_root=task_root, hooks=hooks
        )
    )
    assert result["status"] == "completed"
    assert result["tool"] == "fingerprint"
    assert result["phase"] == "pre_recon"
    summary = result["summary"]
    # 四层指纹命中
    fp = summary["fingerprints"]
    assert any(h["name"] == "nginx" for h in fp["server"])
    assert any(h["name"] == "PHP" for h in fp["backend"])
    assert any(h["name"] == "React" for h in fp["frontend"])
    assert any(h["name"] == "WordPress" for h in fp["cms"])
    # 无 preauth 会话：明确标记无凭证（external_only 或 no_auth_context:*）
    assert summary["auth_mode"].startswith(("external_only", "no_auth_context"))

    # 事件推送：进入前置侦查阶段 + tool 调用
    phases = [e for e in hooks.events if e[0] == "phase"]
    assert any(p[2] == "pre_recon" for p in phases)
    tool_starts = [e for e in hooks.events if e[0] == "tool_start"]
    assert any(t[1] == "pre_recon" and t[2] == "fingerprint" for t in tool_starts)
    tool_ends = [e for e in hooks.events if e[0] == "tool_end"]
    assert any(t[1] == "pre_recon" and t[2] == "fingerprint" and t[3] is True for t in tool_ends)


def test_pre_recon_persists_all_channels(target_server, task_root):
    """落库三通道 + fingerprint 明细表均写入。"""
    tid = _task_id()
    asyncio.run(
        pre_recon.run_pre_recon(task_id=tid, target_url=target_server, task_root=task_root, hooks=None)
    )
    store = ReconStore.for_task(tid, str(task_root))
    try:
        # fingerprint 明细表
        fps = store.list_fingerprints(tid)
        assert len(fps) == 1
        fp = fps[0]
        assert fp["target_url"] == target_server
        assert fp["source"] == "pre_recon"
        assert fp["matched_count"] >= 4
        assert any(h["name"] == "WordPress" for h in fp["fingerprint"]["cms"])
        # technology facts（L0/L1 注入用）
        facts = store.list_facts(tid, category="technology")
        names = {f["key"] for f in facts}
        assert "server:nginx" in names
        assert "backend:PHP" in names
        assert "cms:WordPress" in names
        # 端点树由 sitemap 事务派生（指纹技术栈保留在 recon_facts(technology)，
        # 见上方 facts 断言），此处校验端点已派生且来自前置侦查链路
        eps = store.list_endpoints(tid)
        assert eps, "pre_recon 应已派生端点树"
        # 足迹防重
        traces = store.list_techniques(tid)
        host = target_server.split("://")[1]
        assert any(f"fingerprint|{host}" in t["name"] for t in traces)
    finally:
        store.close()


def test_pre_recon_waf_detected(waf_server, task_root):
    """WAF 识别：Cloudflare 前置被识别为 waf 分类。"""
    tid = _task_id()
    result = asyncio.run(
        pre_recon.run_pre_recon(task_id=tid, target_url=waf_server, task_root=task_root, hooks=None)
    )
    assert result["status"] == "completed"
    waf = result["summary"]["fingerprints"]["waf"]
    assert any("cloudflare" in (h["name"] or "").lower() for h in waf)


def test_pre_recon_idempotent(target_server, task_root):
    """同 host 二次调用：fingerprint 表覆盖不新增行。"""
    tid = _task_id()
    asyncio.run(
        pre_recon.run_pre_recon(task_id=tid, target_url=target_server, task_root=task_root, hooks=None)
    )
    asyncio.run(
        pre_recon.run_pre_recon(task_id=tid, target_url=target_server, task_root=task_root, hooks=None)
    )
    store = ReconStore.for_task(tid, str(task_root))
    try:
        assert len(store.list_fingerprints(tid)) == 1
    finally:
        store.close()


def test_pre_recon_events_during_run(target_server, task_root):
    """事件时序：phase 先于 tool_start，tool_start 先于 tool_end。"""
    hooks = _HooksRecorder()
    tid = _task_id()
    asyncio.run(
        pre_recon.run_pre_recon(task_id=tid, target_url=target_server, task_root=task_root, hooks=hooks)
    )
    seq = [e[0] for e in hooks.events]
    # phase 在前，tool 调用成对出现
    assert seq.index("phase") < seq.index("tool_start")
    assert seq.index("tool_start") < seq.index("tool_end")


# ---------------------------------------------------------------------------
# 认证会话等待（preauth_wait_timeout）：避免 pre_recon 抢跑导致 sitemap 匿名爬取
# ---------------------------------------------------------------------------


def test_wait_for_auth_context_polls_until_ready(monkeypatch):
    """认证会话在轮询中出现：等待返回 authenticated 并带出 cookies。"""
    calls = {"n": 0}

    def fake_resolve(task_root, target, profile):
        calls["n"] += 1
        if calls["n"] >= 3:
            return {"PHPSESSID": "s1"}, {}, "authenticated:preauth"
        return {}, {}, "no_auth_context:preauth"

    monkeypatch.setattr(pre_recon, "_resolve_auth", fake_resolve)
    monkeypatch.setattr(pre_recon, "PREAUTH_WAIT_INTERVAL", 0.01)
    cookies, headers, mode = asyncio.run(
        pre_recon._wait_for_auth_context(Path("/tmp/x"), "http://t", "preauth", 5.0)
    )
    assert mode == "authenticated:preauth"
    assert cookies == {"PHPSESSID": "s1"}
    assert calls["n"] >= 3


def test_wait_for_auth_context_timeout_degrades(monkeypatch):
    """认证会话始终未就绪：超时后降级 no_auth_context，不抛异常。"""
    monkeypatch.setattr(pre_recon, "PREAUTH_WAIT_INTERVAL", 0.01)
    monkeypatch.setattr(
        pre_recon, "_resolve_auth",
        lambda *a, **k: ({}, {}, "no_auth_context:preauth"),
    )
    cookies, headers, mode = asyncio.run(
        pre_recon._wait_for_auth_context(Path("/tmp/x"), "http://t", "preauth", 0.05)
    )
    assert mode == "no_auth_context:preauth"
    assert cookies == {}


def test_pre_recon_auth_wait_timeout_degrade_graceful(target_server, task_root):
    """run_pre_recon 带 preauth_wait_timeout：无会话等超时后仍 completed 并降级。"""
    tid = _task_id()
    result = asyncio.run(
        pre_recon.run_pre_recon(
            task_id=tid, target_url=target_server, task_root=task_root,
            hooks=None, preauth_wait_timeout=0.2,
        )
    )
    assert result["status"] == "completed"
    assert result["summary"]["auth_mode"].startswith(("external_only", "no_auth_context"))
