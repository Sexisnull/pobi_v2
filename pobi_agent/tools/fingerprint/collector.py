"""HTTP 信号采集器：抓取响应头 / HTML / favicon，供指纹匹配使用。

使用 aiohttp（项目已有依赖），``verify_ssl=False``（渗透测试惯例），
跟随重定向，body 截断防超大页面拖垮上下文。favicon 哈希采用 EHole/Finger
同款算法：``mmh3.hash(base64.b64encode(icon_bytes))``（signed 32-bit）。
"""

from __future__ import annotations

import base64
import logging
import re
from dataclasses import dataclass
from urllib.parse import urljoin

import aiohttp
import mmh3

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT: float = 12.0
MAX_BODY_BYTES: int = 1_000_000       # 1MB，防超大页面
MAX_FAVICON_BYTES: int = 512_000      # 512KB
MAX_REDIRECTS: int = 5

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

# 匹配 <link rel="icon" / "shortcut icon" / "apple-touch-icon" href="...">
_FAVICON_RE = re.compile(
    r"""<link\b[^>]*\brel\s*=\s*["']?[^"'>]*(?:icon|apple-touch-icon)[^"'>]*["']?[^>]*\bhref\s*=\s*["']([^"']+)["']""",
    re.IGNORECASE,
)


def favicon_hash_from_bytes(data: bytes) -> int:
    """EHole/Finger 同款 favicon 哈希：mmh3(base64(icon_bytes))。"""
    return mmh3.hash(base64.b64encode(data))


def normalize_target(target: str) -> str:
    """URL 规范化：无 scheme 时补 http://。"""
    t = target.strip().strip('"')
    if not re.match(r"^https?://", t, re.IGNORECASE):
        t = "http://" + t
    return t


def headers_to_text(headers) -> str:
    """响应头序列化为小写 "name: value" 多行文本（供 header 规则匹配）。"""
    lines = []
    for key, value in headers.items():
        lines.append(f"{key.lower()}: {value}")
    return "\n".join(lines)


@dataclass
class CollectedSignals:
    """一次指纹探测采集到的全部信号。"""

    url: str
    final_url: str
    status_code: int | None
    headers: dict[str, str]
    headers_text: str
    body: str
    body_lower: str
    favicon_hash: int | None
    favicon_url: str | None
    server: str | None = None


def _extract_favicon_url(body: str, base_url: str) -> str | None:
    m = _FAVICON_RE.search(body)
    if m:
        return urljoin(base_url, m.group(1).strip())
    return None


async def _fetch_favicon_hash(
    session: aiohttp.ClientSession,
    base_url: str,
    body: str,
) -> tuple[int | None, str | None]:
    """从 HTML 提取 favicon 链接（或回退 /favicon.ico）并计算哈希。"""
    candidates = [_extract_favicon_url(body, base_url)]
    if not candidates[0]:
        candidates.append(urljoin(base_url, "/favicon.ico"))
    for fav_url in candidates:
        if fav_url is None:
            continue
        try:
            async with session.get(fav_url, timeout=aiohttp.ClientTimeout(total=8.0)) as resp:
                if resp.status != 200:
                    continue
                data = await resp.content.readexactly(MAX_FAVICON_BYTES + 1) if resp.content_length and resp.content_length > MAX_FAVICON_BYTES else await resp.read()
            if len(data) > MAX_FAVICON_BYTES:
                data = data[:MAX_FAVICON_BYTES]
            if not data:
                continue
            return favicon_hash_from_bytes(data), fav_url
        except Exception as exc:  # noqa: BLE001 - favicon 获取失败不阻断主探测
            logger.debug("favicon 获取失败 %s: %s", fav_url, exc)
    return None, None


async def collect(
    target: str,
    cookies: dict[str, str] | None = None,
    extra_headers: dict[str, str] | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> CollectedSignals:
    """对目标执行一次指纹信号采集。

    Args:
        target: 目标 URL（自动补 scheme）。
        cookies: 附加 Cookie（认证会话；登录后探测用）。
        extra_headers: 附加请求头（如 auth headers）。
        timeout: 总超时秒数。

    Returns:
        CollectedSignals：未探测到 favicon 时 favicon_hash 为 None。
    """
    target = normalize_target(target)
    connector = aiohttp.TCPConnector(ssl=False, limit=4)
    base_headers = {"User-Agent": _UA, "Accept": "*/*"}
    if extra_headers:
        base_headers.update(extra_headers)

    timeout_obj = aiohttp.ClientTimeout(total=timeout)
    empty = CollectedSignals(
        url=target, final_url=target, status_code=None,
        headers={}, headers_text="", body="", body_lower="",
        favicon_hash=None, favicon_url=None,
    )
    async with aiohttp.ClientSession(
        connector=connector, timeout=timeout_obj, headers=base_headers,
        cookie_jar=aiohttp.DummyCookieJar(),
    ) as session:
        try:
            async with session.get(
                target, cookies=cookies,
                allow_redirects=True, max_redirects=MAX_REDIRECTS,
            ) as resp:
                raw = await resp.read()
                status = resp.status
                final_url = str(resp.url)
                headers = {k: v for k, v in resp.headers.items()}
                body_bytes = raw[:MAX_BODY_BYTES]
                body = body_bytes.decode("utf-8", errors="replace")
        except Exception as exc:  # noqa: BLE001 - 采集失败返回空信号
            logger.debug("指纹主探测失败 %s: %s", target, exc)
            empty.status_code = None
            empty.headers_text = ""
            return empty

        favicon_hash, favicon_url = await _fetch_favicon_hash(
            session, final_url, body
        )
        headers_text = headers_to_text(headers)
        # 降级：scheme 缺失时按响应最终 URL 推断
        return CollectedSignals(
            url=target,
            final_url=final_url,
            status_code=status,
            headers=headers,
            headers_text=headers_text,
            body=body,
            body_lower=body.lower(),
            favicon_hash=favicon_hash,
            favicon_url=favicon_url,
            server=headers.get("Server"),
        )
