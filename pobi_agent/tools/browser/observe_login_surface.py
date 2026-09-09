# Copyright (C) 2025 Yassine Bargach
# Licensed under the GNU Affero General Public License v3
# See LICENSE file for full license information.

"""登录面观察工具（observe_login_surface）。

任务创建阶段（无 recon 上下文）给 LLM 提供登录面的结构化观察结果，
用于决策认证形态（form / json / http / oauth）与表单字段，替代原先
写死的 ``auth_flow="form"`` 分支。

输出是 secret-free 的结构化 JSON：
- HTTP 探测层：是否存在 HTTP Basic 挑战（WWW-Authenticate）、最终状态码。
- 浏览器观察层：最终 URL / 标题、表单 input 字段（type/name/id/placeholder）、
  button、form(action/method)、body 文本摘要、是否跨域重定向（OAuth 特征）、
  是否有密码输入框。

不返回任何 cookie 值、输入框 value 或凭据。脚本遵守 Pydoll 约束：
多语句 + return + JSON.stringify 字符串返回。
"""
from __future__ import annotations

import json
import ssl
from typing import Any

import aiohttp
from pydantic_ai import RunContext

from pobi_agent.logging import logger
from pobi_agent.tools.browser.browser import BrowserSession
from pobi_agent.tools.tool_wrappers import with_tool_events
from pobi_agent.utils.structures import PreAuthDeps

# 浏览器内提取登录面结构：多语句 + return + JSON.stringify（Pydoll execute_script 约束，
# 对象返回值会退化为 objectId 无法取值；禁止箭头函数对象字面量）。
_OBSERVE_JS = r"""
const inputs = Array.from(document.querySelectorAll('input')).map(function (i) {
  return {
    type: i.type || '',
    name: i.name || '',
    id: i.id || '',
    placeholder: i.placeholder || '',
    autocomplete: i.autocomplete || '',
    required: !!i.required
  };
});
const buttons = Array.from(document.querySelectorAll('button, input[type=submit], input[type=button]')).map(function (b) {
  return {
    tag: b.tagName.toLowerCase(),
    type: b.type || '',
    text: (b.textContent || b.value || '').trim().slice(0, 50)
  };
});
const forms = Array.from(document.querySelectorAll('form')).map(function (f) {
  return {
    action: f.getAttribute('action') || '',
    method: (f.method || 'get').toLowerCase()
  };
});
const bodyText = (document.body ? document.body.innerText : '').replace(/\s+/g, ' ').trim().slice(0, 1500);
const hasPw = !!document.querySelector('input[type=password]');
return JSON.stringify({
  inputs: inputs,
  buttons: buttons,
  forms: forms,
  bodyText: bodyText,
  hasPw: hasPw
});
"""


def _build_connector(verify_ssl: bool) -> aiohttp.TCPConnector:
    if verify_ssl:
        return aiohttp.TCPConnector()
    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE
    return aiohttp.TCPConnector(ssl=ssl_ctx)


async def _probe_http_basic(
    url: str,
    verify_ssl: bool,
    timeout_s: float = 10.0,
) -> dict[str, Any]:
    """无浏览器 GET 探测：识别 HTTP Basic 挑战与响应特征（失败不阻断）。"""
    out: dict[str, Any] = {
        "http_basic": False,
        "probe_status": None,
        "probe_content_type": None,
        "probe_final_url": None,
    }
    try:
        connector = _build_connector(verify_ssl)
        timeout = aiohttp.ClientTimeout(total=timeout_s)
        async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
            async with session.get(url, allow_redirects=True) as resp:
                www_auth = resp.headers.get("WWW-Authenticate", "")
                out["http_basic"] = bool(www_auth)
                out["probe_status"] = resp.status
                out["probe_content_type"] = resp.headers.get("Content-Type", "")
                out["probe_final_url"] = str(resp.url)
    except Exception as exc:  # noqa: BLE001 — 探测失败不阻断浏览器观察
        logger.warning("observe_login_surface: HTTP probe failed: %s", exc)
    return out


async def _observe_login_surface(
    *,
    url: str,
    proxy_url: str | None = None,
    verify_ssl: bool = False,
    navigation_timeout_ms: float | None = 25_000,
) -> dict[str, Any]:
    """浏览器导航到登录页并提取登录面结构。"""
    try:
        async with BrowserSession(
            headless=True,
            verify_ssl=verify_ssl,
            proxy_url=proxy_url,
        ) as browser:
            await browser.goto(url, timeout_ms=navigation_timeout_ms)
            final_url = await browser.get_url()
            title = await browser.get_title()
            raw = await browser.execute_script(_OBSERVE_JS)
            parsed: dict[str, Any] = {}
            if isinstance(raw, str):
                try:
                    parsed = json.loads(raw)
                except json.JSONDecodeError:
                    parsed = {}
            if not isinstance(parsed, dict):
                parsed = {}
            # 跨域重定向 → OAuth / 第三方 IdP 特征
            parsed["oauth_redirect"] = _host(final_url) != _host(url)
            parsed["title"] = title or ""
            parsed["final_url"] = final_url
            return {"success": True, **parsed}
    except Exception as exc:  # noqa: BLE001 — 观察失败归类为失败返回
        logger.warning("observe_login_surface: browser observe failed: %s", exc)
        return {"success": False, "error": f"登录面观察失败: {exc}"}


def _host(url: str) -> str:
    try:
        from urllib.parse import urlparse

        return urlparse(url).netloc.lower()
    except Exception:  # noqa: BLE001 — 解析失败按空处理
        return ""


@with_tool_events("observe_login_surface")
async def observe_login_surface(
    ctx: RunContext[PreAuthDeps],
    login_url: str | None = None,
    navigation_timeout_ms: float | None = 25_000,
) -> dict[str, Any]:
    """导航到登录地址，返回登录面结构化观察结果（secret-free）。

    在调用 ``authenticate`` 之前先观察：判断目标是 HTML 表单登录、JSON API
    登录、HTTP Basic 还是 OAuth 重定向，并获取真实表单字段 / 按钮 / 提交方式。

    **入参**：

    - ``login_url`` (str, 可选)：登录页 URL。缺省用任务配置的登录地址，再缺省
      用目标 URL。
    - ``navigation_timeout_ms`` (int, 可选)：页面导航超时毫秒数，默认 25000。

    **返回**：

    - ``success`` (bool)：观察是否成功。
    - ``final_url`` / ``title``：登录后/重定向后的最终地址与页面标题。
    - ``http_basic`` (bool)：目标存在 HTTP Basic 挑战（WWW-Authenticate 头）。
    - ``oauth_redirect`` (bool)：页面最终地址与请求地址不同源（疑似 OAuth /
      第三方登录重定向）。
    - ``has_pw`` (bool)：页面是否有密码输入框。
    - ``inputs`` (list)：所有 ``<input>`` 的 type/name/id/placeholder/autocomplete/required。
    - ``buttons`` (list)：所有 button / submit 按钮的 tag/type/可见文本。
    - ``forms`` (list)：所有 ``<form>`` 的 action/method。
    - ``body_text`` (str)：页面可见文本摘要（最多 1500 字符）。
    - ``error`` (str)：失败原因（仅失败时）。

    **安全**：本工具绝不返回 cookie 值、输入框 value 或任何凭据。
    """
    url = login_url or ctx.deps.login_url or ctx.deps.target
    if not url:
        return {"success": False, "error": "缺少登录地址（login_url）"}

    probe = await _probe_http_basic(url, ctx.deps.verify_ssl)
    observed = await _observe_login_surface(
        url=url,
        proxy_url=ctx.deps.proxy_url,
        verify_ssl=ctx.deps.verify_ssl,
        navigation_timeout_ms=navigation_timeout_ms,
    )
    if not observed.get("success"):
        return {**observed, **probe}
    return {**observed, **probe}


__all__ = ["observe_login_surface"]
