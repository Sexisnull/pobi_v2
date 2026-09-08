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
    # 日志目录：容器内默认 /app/logs；宿主机开发/测试可经 POBI_V2_LOG_DIR 覆盖，
    # 避免模块顶层硬编码容器路径导致本地无法 import 应用（历史问题）。
    log_dir: str = "/app/logs"

    # ---- 数据库 ----
    database_url: str = "postgresql+psycopg://pobi:pobi@localhost:5432/pobi_v2"

    # ---- 事件总线 / 任务队列（M2 启用）----
    redis_url: str = "redis://localhost:6379/0"
    # 事件总线后端：memory（开发）| redis（生产，支持多 worker）
    event_bus_backend: str = "memory"

    # ---- LLM（统一前缀；唯一解析入口见 pobi_v2.llm.get_model_spec）----
    # model: "provider/model" 串，provider 段仅决定 litellm 路由前缀。
    # 凭证一律走 llm_api_key / llm_api_base（POBI_V2_LLM_API_KEY / POBI_V2_LLM_API_BASE）；
    # 缺失时由统一入口按 provider 回退裸供应商变量（如 ANTHROPIC_API_KEY）兼容。
    model: str = "openai/gpt-4o-mini"
    llm_api_base: str | None = None
    llm_api_key: str | None = None
    llm_rate_limit_rpm: int = 60
    # LLM 单调用超时（秒）：远端无响应时防止协程无限 await 假死；超时后由 litellm
    # 按 llm_max_retries 自动退避重试，仍失败则任务报错终止（而非等 30 分钟子超时）。
    llm_request_timeout: int = 180
    # LLM 调用自动重试次数（覆盖瞬态：超时 / 限流 429 / 5xx / 连接失败）
    llm_max_retries: int = 3

    # DEPRECATED: 与 `model` 语义重叠，统一入口不再消费。
    # 模型串请只用 `model`（POBI_V2_MODEL）。保留仅为向后兼容既有 .env。
    llm_model: str | None = None
    # 是否允许扫描任务执行受限 shell（高危，默认关闭）
    allow_shell_exec: bool = False
    # 授权靶场自动化：为真时审批回调自动批准高危工具调用（默认 fail-closed 仍为拒绝）
    auto_approve: bool = False

    # ---- 任务执行默认上限（M2 使用）----
    task_max_turns: int = 50
    # DeadEndAgent 单次调用子超时（秒）：低于该值视为未收敛死循环，熔断标记 failed。
    # 默认 55 分钟：完整 DVWA 全量利用需 威胁建模(~5min)+利用(~45min)+报告(~5min)，
    # 1800s 原默认在利用阶段即被熔断（2026-09-07 实测 2 次，均在 30min 整熔断）。
    # 任务级硬上限由 ARQ job_timeout（默认 6h）兜底。
    agent_sub_timeout_seconds: int = 120 * 60

    # ---- Worker 并发（方案 A+B：多任务并行消费）----
    # 单个 ARQ Worker 进程内同时执行的任务协程数（ARQ 参数 max_jobs，默认 10）。
    # 因共享 Kali 沙箱为单容器，进程内并发过大会互相争抢 shell/python 资源，
    # 故默认收敛为 2；多任务并行优先靠「多 Worker 副本」(docker-compose replicas)
    # 而非单进程内大并发实现。设为 1 即进程内严格串行。
    worker_max_jobs: int = 2

    # ---- LLM 全局并发上限（防止多任务并行触发上游 429 雪崩）----
    # 多任务并行时 LLM 调用 QPS 线性放大，易触发 RateLimitError。
    # 进程级 asyncio.Semaphore 统一限流（与 task_id 无关，按全局并发卡口），
    # 超出则排队等待，避免 tenacity 指数退避雪崩拖慢整体。
    llm_max_concurrency: int = 8

    # ---- 沙箱镜像 ----
    # 渗透验证在 Kali 沙箱中执行（含 sqlmap/nmap 等工具链）。
    # 默认使用本地已下载的 xoxruns/sandboxed_kali，可通过环境变量覆盖。
    sandbox_image: str = "xoxruns/sandboxed_kali:latest"

    # ---- 沙箱网络（全面容器化）----
    # 所有组件（api/worker/postgres/redis/kali 沙箱）接入的统一 bridge 网络。
    # 容器内经 DooD（挂载 /var/run/docker.sock）复用宿主机 Docker daemon，
    # 跨容器访问使用服务名（postgres/redis/api），不再使用 host 网络。
    sandbox_network: str = "pobi_net"

    # ---- 全局共享 Kali 容器名 ----
    # 常驻单实例 Kali 容器：shell 命令与 Python 验证共用，生命周期随 docker-compose。
    # API/Worker 各自持有一个连接，底层指向同一容器。
    kali_container_name: str = "pobi_kali"

    # ---- M4 鉴权 ----
    jwt_secret: str = "dev-insecure-change-me-please-set-env-secret-32b-plus"
    # 注册开关：生产环境关闭开放注册
    allow_open_registration: bool = True
    # PAT 加密密钥（POBI_V2_TOKEN_ENCRYPTION_KEY）：用于持久化加密 API 令牌明文，
    # 支撑前端「点击查看」。未配置时令牌仍可创建与校验，但创建后不可再 reveal。
    token_encryption_key: str = ""

    # ---- 启动时自动创建的 admin 账号（仅在库中无任何用户时 seed）----
    admin_email: str = "admin@example.com"
    admin_password: str = "admin123456"
    admin_full_name: str = "Administrator"
    # admin 归属的默认租户 slug
    admin_tenant_slug: str = "default"


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
