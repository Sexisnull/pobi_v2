"""URL / path 归一化工具（sitemap 与 requester 共用，保证数据对齐）。

前置侦查 sitemap（katana）与 requester agent 通过同一套归一化规则写入
``recon_http_transactions`` / ``recon_endpoints``，确保：
- ``path_normalized`` 幂等键口径一致（去锚点、去 query、百分号解码、折叠斜杠）；
- 静态资源 / 无意义路径判定在 katana 参数层之外提供统一落库防线。

本模块不依赖 RunContext，纯函数可单测。
"""

from __future__ import annotations

import re
from urllib.parse import urlparse, unquote

# 静态资源扩展名黑名单（无意义页面过滤 · 落库第二层防线）。
STATIC_EXTENSIONS: frozenset[str] = frozenset(
    {
        # 前端资源
        "css", "scss", "less", "js", "mjs", "map", "json", "svg", "ico",
        # 图片
        "png", "jpg", "jpeg", "gif", "webp", "bmp", "avif", "tiff", "heic",
        # 字体
        "woff", "woff2", "ttf", "eot", "otf",
        # 媒体 / 下载
        "pdf", "zip", "gz", "tar", "7z", "rar", "mp4", "mp3", "webm", "ogg",
        "avi", "mov", "wav", "flv",
    }
)

# 无意义路径特征（登出 / 错误 / 会话噪音）。注意：/login 本身是攻击面，
# 但 /login?redirect= 这类会话跳转噪音与 404/error 页不做重点测试。
NOISE_PATH_RE = re.compile(
    r"/(?:logout|signout|forgot-password|reset-password|captcha|error|404)"
    r"(?:[/?#]|$)",
    re.IGNORECASE,
)

# 分页 / 排序 / 日历等参数化噪音 key（配合 katana -iqp 的落库兜底）。
NOISE_PARAM_KEYS: frozenset[str] = frozenset(
    {
        "page", "p", "offset", "limit", "per_page", "size", "sort", "order",
        "orderby", "sortby", "filter", "month", "year", "date", "callback",
        "_", "lang", "locale", "csrf_token", "view",
    }
)


def split_url(url: str) -> tuple[str, str, str, str, dict[str, str]]:
    """把 URL 拆为 (host, path, query, fragment, params_dict)。

    host 为 netloc（含端口）；path 保证以 ``/`` 开头；query 保留原始串；
    params_dict 解析 query 键值（便于事务表 detected_params / 参数化噪音判定）。
    """
    parsed = urlparse((url or "").strip())
    host = parsed.netloc or ""
    path = parsed.path or "/"
    if not path.startswith("/"):
        path = "/" + path
    params: dict[str, str] = {}
    if parsed.query:
        for kv in parsed.query.split("&"):
            if not kv:
                continue
            if "=" in kv:
                k, _, v = kv.partition("=")
                params[k] = v
            else:
                params[kv] = ""
    return host, path, parsed.query, parsed.fragment, params


def _path_of(url_or_path: str) -> str:
    """兼容传入完整 URL 或纯 path，统一返回 path。"""
    if "://" in url_or_path:
        return split_url(url_or_path)[1]
    path = url_or_path or "/"
    return path if path.startswith("/") else "/" + path


def normalize_path(url: str) -> str:
    """从完整 URL 提取并归一化 path（去锚点、去 query、百分号解码、折叠斜杠）。

    与 recon_endpoints.path_normalized 幂等键口径对齐。
    """
    _, path, _, _, _ = split_url(url)
    path = unquote(path)
    path = re.sub(r"/{2,}", "/", path)
    return path or "/"


def is_static_asset(url_or_path: str) -> bool:
    """是否命中静态资源扩展名（katana 参数层之外的第二层防线）。"""
    path = _path_of(url_or_path)
    dot = path.rfind(".")
    if dot == -1:
        return False
    ext = path[dot + 1:].split("/")[0].lower()
    return ext in STATIC_EXTENSIONS


def is_noise_path(url_or_path: str) -> bool:
    """是否命中无意义路径特征（登出 / 错误 / 会话噪音）。"""
    return bool(NOISE_PATH_RE.search(_path_of(url_or_path)))


def has_noise_params(url: str) -> bool:
    """query 参数是否仅含分页 / 排序等噪音 key（无业务参数）。"""
    _, _, _, _, params = split_url(url)
    if not params:
        return False
    # 全部 key 都是噪音 → 该 URL 大概率是分页/排序变体，不值得单独测试。
    return all(k.lower() in NOISE_PARAM_KEYS for k in params)


def extract_params(url: str) -> list[str]:
    """提取 query 参数名列表（供端点 parameters 字段）。"""
    _, _, _, _, params = split_url(url)
    return sorted(params.keys())
