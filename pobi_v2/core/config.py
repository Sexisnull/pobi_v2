"""应用配置（pydantic-settings）。

统一取代原 pobi 的 DEADEND_* 环境变量与 settings.py 的散落配置。
"""
from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="POBI_V2_",
        env_file=".env",
        extra="ignore",
    )

    # ---- 应用 ----
    app_name: str = "Pobi v2"
    debug: bool = False

    # ---- 数据库 ----
    database_url: str = "postgresql+psycopg://pobi:pobi@localhost:5432/pobi_v2"

    # ---- 事件总线 / 任务队列（M2 启用）----
    redis_url: str = "redis://localhost:6379/0"
    # 事件总线后端：memory（开发）| redis（生产，支持多 worker）
    event_bus_backend: str = "memory"

    # ---- LLM（透传给 pobi_agent.CoreAgent）----
    model: str = "openai/gpt-4o-mini"
    llm_api_base: str | None = None
    llm_api_key: str | None = None
    llm_rate_limit_rpm: int = 60
    # 是否允许扫描任务执行受限 shell（高危，默认关闭）
    allow_shell_exec: bool = False

    # ---- 任务执行默认上限（M2 使用）----
    task_max_turns: int = 50

    # ---- M4 鉴权 ----
    jwt_secret: str = "dev-insecure-change-me"
    # 注册开关：生产环境关闭开放注册
    allow_open_registration: bool = True


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
