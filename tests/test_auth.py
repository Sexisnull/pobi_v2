"""M4 鉴权逻辑测试（不依赖 Postgres，覆盖安全原语与路由保护）。"""
from __future__ import annotations

import pytest

from pobi_v2.core.security import (
    create_access_token,
    decode_access_token,
    hash_password,
    verify_password,
)


def test_password_hash_verify():
    h = hash_password("supersecret")
    assert h != "supersecret"
    assert verify_password("supersecret", h) is True
    assert verify_password("wrong", h) is False


def test_password_hash_unique_salt():
    h1 = hash_password("same")
    h2 = hash_password("same")
    assert h1 != h2
    assert verify_password("same", h1)
    assert verify_password("same", h2)


def test_jwt_roundtrip():
    token = create_access_token("user-123", "tenant-456")
    payload = decode_access_token(token)
    assert payload["sub"] == "user-123"
    assert payload["tenant_id"] == "tenant-456"
    assert "exp" in payload


def test_jwt_tamper_rejected():
    import jwt
    from jwt.exceptions import InvalidTokenError

    token = create_access_token("u", "t")
    # 篡改签名部分
    bad = token[:-3] + "xyz"
    try:
        decode_access_token(bad)
        assert False, "tampered token should fail"
    except InvalidTokenError:
        pass


def _pg_reachable() -> bool:
    """检测配置的 PostgreSQL 目标是否可达。

    项目 docker-compose 默认**不映射 PG 宿主端口**（仅 pobi_net 内部直连，
    安全收敛），故宿主机跑 pytest 时默认不可达 → 此类走完整应用栈的集成测试
    跳过；经 docker-compose override 映射端口、或在容器内运行 pytest、
    或经 POBI_TEST_PG_URL / POBI_V2_DATABASE_URL 指向可达 PG 时才运行。
    """
    import socket

    from pobi_v2.core.config import settings

    netloc = settings.database_url.split("://", 1)[-1].split("@")[-1]
    host, _, port_part = netloc.partition(":")
    port = 5432
    if port_part:
        port = int(port_part.split("/")[0])
    try:
        with socket.create_connection((host, port), timeout=1):
            return True
    except OSError:
        return False


@pytest.mark.skipif(
    not _pg_reachable(),
    reason="需本地 PostgreSQL 才能运行（受保护路由走完整应用栈，lifespan 需 seed admin）",
)
def test_protected_route_requires_auth():
    from fastapi.testclient import TestClient

    from pobi_v2.main import app

    # 未携带令牌访问受保护端点应 401
    with TestClient(app) as client:
        resp = client.get("/api/v1/targets")
        assert resp.status_code == 401
        # 公开鉴权端点可用
        login = client.post("/api/v1/auth/login", json={"email": "x", "password": "y"})
        assert login.status_code in (401, 422)  # 无用户=401，schema错=422，均非200
