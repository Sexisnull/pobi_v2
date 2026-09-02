"""webapp_fingerprint 指纹识别工具测试。

覆盖：
1. 规则引擎单元测试（keyword AND / faviconhash / regula）
2. 四层分层判定（server / backend / frontend）
3. 信号采集（collector，对本地 HTTP 靶标）
4. 端到端 webapp_fingerprint（本地靶标 + 轻量 ctx + task_root 落库验证）
"""

from __future__ import annotations

import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from pobi_agent.recon import ReconStore
from pobi_agent.storage_context import clear_task_root, set_task_root
from pobi_agent.tools.fingerprint import webapp_fingerprint
from pobi_agent.tools.fingerprint.collector import collect
from pobi_agent.tools.fingerprint.detectors import detect_layers
from pobi_agent.tools.fingerprint.matcher import match_rules
from pobi_agent.utils.structures import RequesterDeps

FAVICON_BYTES = b"\x89PNG\r\n\x1a\n" + b"X" * 64


class _TargetHandler(BaseHTTPRequestHandler):
    """模拟 nginx + PHP + WordPress + React 靶标。"""

    def log_message(self, *args):  # 静音访问日志
        pass

    def _favicon(self):
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.end_headers()
        self.wfile.write(FAVICON_BYTES)

    def _index(self):
        self.send_response(200)
        self.send_header("Server", "nginx/1.24.0")
        self.send_header("X-Powered-By", "PHP/7.4.33")
        self.send_header("Set-Cookie", "PHPSESSID=abc123; path=/")
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        body = (
            "<html><head><link rel='icon' href='/favicon.ico'></head><body>"
            "<div id='root' data-reactroot=''></div>"
            "<script src='/wp-includes/js/jquery.min.js'></script>"
            "<img src='/wp-content/themes/theme/style.css'>"
            "<a href='/wp-admin/'>admin</a>"
            "</body></html>"
        )
        self.wfile.write(body.encode())

    def do_GET(self):
        if self.path == "/favicon.ico":
            self._favicon()
        else:
            self._index()


class _WafHandler(BaseHTTPRequestHandler):
    """模拟 Cloudflare 保护站点。"""

    def log_message(self, *args):  # 静音访问日志
        pass

    def do_GET(self):
        self.send_response(200)
        self.send_header("Server", "cloudflare")
        self.send_header("cf-ray", "7f2a3b4c-ORD")
        self.send_header("Set-Cookie", "__cf_bm=xyz; path=/")
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(b"<html>protected</html>")


@pytest.fixture()
def target_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _TargetHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


@pytest.fixture()
def waf_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _WafHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def _make_ctx(target: str, tmp_path):
    """构造最小可用的 requester ctx（deps 齐全但工具仅消费 session_id/agent_id/target）。"""
    session_id = uuid.uuid4()
    deps = RequesterDeps(
        embedder_client=None,
        rag=None,
        target=target,
        agent_id=uuid.uuid4(),
        session_id=session_id,
    )
    token = set_task_root(tmp_path)
    try:
        yield SimpleNamespace(deps=deps), str(session_id)
    finally:
        clear_task_root(token)


# ---------------------------------------------------------------------------
# 1. 规则引擎单元测试
# ---------------------------------------------------------------------------


def test_keyword_and_all_required():
    rule = {
        "name": "WordPress", "method": "keyword", "location": "body",
        "keywords": ["wp-content/", "wp-admin"], "category": "cms", "confidence": 0.8,
    }
    # 两个关键字都在 → 命中
    assert match_rules([rule], "", "wp-content/themes/x and wp-admin", None)
    # 只出现一个 → 不命中（AND 语义）
    assert not match_rules([rule], "", "wp-content/themes/x", None)


def test_faviconhash_match():
    rule = {
        "name": "spring-boot", "method": "faviconhash", "location": "body",
        "keywords": ["116323821"], "category": "cms", "confidence": 0.9,
    }
    hits = match_rules([rule], "", "", favicon_hash=116323821)
    assert hits and hits[0].name == "spring-boot"
    assert not match_rules([rule], "", "", favicon_hash=999999)


def test_regula_match():
    rule = {
        "name": "weblogic", "method": "regula", "location": "body",
        "keywords": [r"weblogic[.-]?\d+"], "category": "cms", "confidence": 0.7,
    }
    assert match_rules([rule], "", "powered by weblogic.10.3", None)
    assert not match_rules([rule], "", "powered by tomcat", None)


# ---------------------------------------------------------------------------
# 2. 四层分层判定
# ---------------------------------------------------------------------------


def test_detect_layers_nginx_php_react():
    headers = "server: nginx/1.24\nx-powered-by: php/7.4\nset-cookie: phpsessid=1"
    body = '<div data-reactroot=""></div><script src="/js/jquery.min.js"></script>'
    hits = detect_layers(headers, body)
    cats = {(h.category, h.name) for h in hits}
    assert ("server", "nginx") in cats
    assert ("backend", "PHP") in cats
    assert ("frontend", "React") in cats
    assert ("frontend", "jQuery") in cats


# ---------------------------------------------------------------------------
# 3. 信号采集
# ---------------------------------------------------------------------------


async def test_collect_headers_and_body(target_server):
    signals = await collect(target_server)
    assert signals.status_code == 200
    assert signals.server == "nginx/1.24.0"
    assert "x-powered-by: PHP" in signals.headers_text
    assert "data-reactroot" in signals.body
    assert signals.favicon_hash is not None


# ---------------------------------------------------------------------------
# 4. 端到端 webapp_fingerprint（外部探测 + 落库）
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_webapp_fingerprint_endtoend(target_server, tmp_path):
    gen = _make_ctx(target_server, tmp_path)
    ctx, session_id = next(gen)
    try:
        result = await webapp_fingerprint(ctx, target_server)
    finally:
        next(gen, None)

    assert result["status_code"] == 200
    assert result["auth_mode"] == "external_only"
    assert result["cached"] is False

    fp = result["fingerprints"]
    assert any(h["name"] == "nginx" for h in fp["server"])
    assert any(h["name"] == "PHP" for h in fp["backend"])
    assert any(h["name"] == "WordPress" for h in fp["cms"])
    assert any(h["name"] == "React" for h in fp["frontend"])
    assert "无可用登录凭证" in result["auth_hint"]

    # 落库验证：recon_facts 有 technology 事实，recon_techniques 有足迹
    db = tmp_path / f"{session_id}.db"
    assert db.exists()
    store = ReconStore(db)
    try:
        facts = store.lookup(session_id, category="technology", limit=100)
        assert any("cms:WordPress" == f["key"] for f in facts)
        traces = store.lookup(session_id, tech=f"fingerprint|127.0.0.1:{target_server.rsplit(':', 1)[1]}", limit=5)
        assert any(
            r.get("kind") == "technique" and "fingerprint|" in (r.get("name") or "")
            for r in traces
        )
    finally:
        store.close()


@pytest.mark.asyncio
async def test_webapp_fingerprint_second_call_cached(waf_server, tmp_path):
    """同 host 二次调用应命中足迹防重，不再发请求（cached=True）。"""
    gen = _make_ctx(waf_server, tmp_path)
    ctx, _ = next(gen)
    try:
        first = await webapp_fingerprint(ctx, waf_server)
        second = await webapp_fingerprint(ctx, waf_server)
    finally:
        next(gen, None)

    assert first["cached"] is False
    assert second["cached"] is True
    assert any(h["name"] == "cloudflare" for h in first["fingerprints"]["waf"])
    assert "已完成指纹识别" in second["hint"]


@pytest.mark.asyncio
async def test_webapp_fingerprint_unreachable(tmp_path):
    """不可达目标应返回错误且不崩。"""
    gen = _make_ctx("http://127.0.0.1:1", tmp_path)
    ctx, _ = next(gen)
    try:
        result = await webapp_fingerprint(ctx, "http://127.0.0.1:1")
    finally:
        next(gen, None)
    assert "error" in result
