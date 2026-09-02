"""server / backend / frontend 分层判定特征规则（内建，不依赖外部指纹库）。

与 cms/waf 库规则共用 matcher 引擎，但按语义分层组织：
- SERVER_RULES:   Web 服务器指纹（主要看 Server / X-Powered-By 等响应头）
- BACKEND_RULES:  后端语言/框架指纹（Cookie、框架专属响应头）
- FRONTEND_RULES: 前端框架指纹（HTML body 中的框架产物信号）

规则刻意使用较特异的信号降低误报（如 ``data-reactroot`` 而非泛化的 ``react``）。
"""

from __future__ import annotations

from .matcher import FingerprintHit, match_rules

# category=server —— 响应头 Server 特征
SERVER_RULES: list[dict] = [
    {"name": "nginx", "method": "keyword", "location": "header", "keywords": ["server: nginx"], "category": "server", "confidence": 0.85},
    {"name": "openresty", "method": "keyword", "location": "header", "keywords": ["server: openresty"], "category": "server", "confidence": 0.9},
    {"name": "tengine", "method": "keyword", "location": "header", "keywords": ["server: tengine"], "category": "server", "confidence": 0.9},
    {"name": "apache", "method": "keyword", "location": "header", "keywords": ["server: apache"], "category": "server", "confidence": 0.85},
    {"name": "iis", "method": "keyword", "location": "header", "keywords": ["server: microsoft-iis"], "category": "server", "confidence": 0.9},
    {"name": "tomcat", "method": "keyword", "location": "header", "keywords": ["server: apache-coyote"], "category": "server", "confidence": 0.9},
    {"name": "jetty", "method": "keyword", "location": "header", "keywords": ["server: jetty"], "category": "server", "confidence": 0.9},
    {"name": "caddy", "method": "keyword", "location": "header", "keywords": ["server: caddy"], "category": "server", "confidence": 0.9},
    {"name": "gunicorn", "method": "keyword", "location": "header", "keywords": ["server: gunicorn"], "category": "server", "confidence": 0.9},
    {"name": "uvicorn", "method": "keyword", "location": "header", "keywords": ["server: uvicorn"], "category": "server", "confidence": 0.9},
    {"name": "werkzeug", "method": "keyword", "location": "header", "keywords": ["server: werkzeug"], "category": "server", "confidence": 0.9},
    {"name": "cloudflare", "method": "keyword", "location": "header", "keywords": ["server: cloudflare"], "category": "server", "confidence": 0.85},
    {"name": "yunjiasu-nginx", "method": "keyword", "location": "header", "keywords": ["server: yunjiasu-nginx"], "category": "server", "confidence": 0.9},
    {"name": "cdnetworks", "method": "keyword", "location": "header", "keywords": ["server: cdnetworks"], "category": "server", "confidence": 0.85},
]

# category=backend —— 后端语言/框架
BACKEND_RULES: list[dict] = [
    {"name": "PHP", "method": "keyword", "location": "header", "keywords": ["x-powered-by: php"], "category": "backend", "confidence": 0.85},
    {"name": "PHP", "method": "keyword", "location": "header", "keywords": ["set-cookie: phpsessid"], "category": "backend", "confidence": 0.85},
    {"name": "Java", "method": "keyword", "location": "header", "keywords": ["set-cookie: jsessionid"], "category": "backend", "confidence": 0.85},
    {"name": "Java", "method": "keyword", "location": "header", "keywords": ["x-powered-by: servlet"], "category": "backend", "confidence": 0.85},
    {"name": "Java", "method": "keyword", "location": "header", "keywords": ["x-powered-by: jsp"], "category": "backend", "confidence": 0.8},
    {"name": "ASP.NET", "method": "keyword", "location": "header", "keywords": ["x-aspnet-version"], "category": "backend", "confidence": 0.95},
    {"name": "ASP.NET", "method": "keyword", "location": "header", "keywords": ["set-cookie: asp.net_sessionid"], "category": "backend", "confidence": 0.9},
    {"name": "Python/WSGI", "method": "keyword", "location": "header", "keywords": ["x-powered-by: python"], "category": "backend", "confidence": 0.8},
    {"name": "Python/WSGI", "method": "keyword", "location": "header", "keywords": ["wsgi"], "category": "backend", "confidence": 0.6},
    {"name": "Django", "method": "keyword", "location": "header", "keywords": ["set-cookie: csrftoken"], "category": "backend", "confidence": 0.9},
    {"name": "Express/Node", "method": "keyword", "location": "header", "keywords": ["x-powered-by: express"], "category": "backend", "confidence": 0.95},
    {"name": "ThinkPHP", "method": "keyword", "location": "header", "keywords": ["x-powered-by: thinkphp"], "category": "backend", "confidence": 0.9},
    {"name": "Laravel", "method": "keyword", "location": "header", "keywords": ["set-cookie: laravel_session"], "category": "backend", "confidence": 0.9},
    {"name": "Ruby on Rails", "method": "keyword", "location": "header", "keywords": ["x-powered-by: phusion passenger"], "category": "backend", "confidence": 0.85},
    {"name": "Ruby on Rails", "method": "keyword", "location": "header", "keywords": ["_session_id"], "category": "backend", "confidence": 0.7},
    {"name": "Apache Shiro", "method": "keyword", "location": "header", "keywords": ["set-cookie: rememberme"], "category": "backend", "confidence": 0.9},
    {"name": "Gin/Go", "method": "keyword", "location": "header", "keywords": ["x-powered-by: gin"], "category": "backend", "confidence": 0.9},
]

# category=frontend —— 前端框架（body 信号）
FRONTEND_RULES: list[dict] = [
    {"name": "React", "method": "keyword", "location": "body", "keywords": ["data-reactroot"], "category": "frontend", "confidence": 0.9},
    {"name": "React", "method": "keyword", "location": "body", "keywords": ["react.production"], "category": "frontend", "confidence": 0.8},
    {"name": "Next.js", "method": "keyword", "location": "body", "keywords": ["__next_data__"], "category": "frontend", "confidence": 0.85},
    {"name": "Next.js", "method": "keyword", "location": "body", "keywords": ["__next"], "category": "frontend", "confidence": 0.8},
    {"name": "Vue", "method": "keyword", "location": "body", "keywords": ["vue@3"], "category": "frontend", "confidence": 0.85},
    {"name": "Vue", "method": "keyword", "location": "body", "keywords": ["vue@2"], "category": "frontend", "confidence": 0.85},
    {"name": "Vue", "method": "keyword", "location": "body", "keywords": ["__vue__"], "category": "frontend", "confidence": 0.85},
    {"name": "Vue", "method": "keyword", "location": "body", "keywords": ["vue.global"], "category": "frontend", "confidence": 0.8},
    {"name": "Angular", "method": "keyword", "location": "body", "keywords": ["ng-version"], "category": "frontend", "confidence": 0.95},
    {"name": "jQuery", "method": "keyword", "location": "body", "keywords": ["jquery.min.js"], "category": "frontend", "confidence": 0.85},
    {"name": "jQuery", "method": "keyword", "location": "body", "keywords": ["jquery.js"], "category": "frontend", "confidence": 0.8},
    {"name": "Bootstrap", "method": "keyword", "location": "body", "keywords": ["bootstrap.bundle"], "category": "frontend", "confidence": 0.75},
    {"name": "Bootstrap", "method": "keyword", "location": "body", "keywords": ["bootstrap.min.css"], "category": "frontend", "confidence": 0.75},
    {"name": "webpack", "method": "keyword", "location": "body", "keywords": ["__webpack_require__"], "category": "frontend", "confidence": 0.9},
    {"name": "Vite", "method": "keyword", "location": "body", "keywords": ["/vite.svg"], "category": "frontend", "confidence": 0.8},
    {"name": "Nuxt", "method": "keyword", "location": "body", "keywords": ["__nuxt"], "category": "frontend", "confidence": 0.85},
    {"name": "Svelte", "method": "keyword", "location": "body", "keywords": ["svelte"], "category": "frontend", "confidence": 0.6},
]

LAYER_RULES: list[dict] = SERVER_RULES + BACKEND_RULES + FRONTEND_RULES


def detect_layers(
    headers_text: str,
    body_lower: str,
    favicon_hash: int | None = None,
) -> list[FingerprintHit]:
    """对 server / backend / frontend 三层内建特征规则统一匹配。"""
    return match_rules(LAYER_RULES, headers_text, body_lower, favicon_hash)
