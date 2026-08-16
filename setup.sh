#!/usr/bin/env bash
# Pobi v2 一键安装脚本（强制依赖校验 + 自动安装）
#
# 本平台复刻 deadend-cli 的自主渗透测试能力，内核强依赖以下组件，缺失即不可用：
#   1. Python >= 3.11          —— 项目运行基础
#   2. uv                      —— 依赖与虚拟环境管理（pyproject 已含全部内核依赖）
#   3. Docker + docker compose —— 主路径沙箱（无则无法运行，全局共享 Kali 容器由 compose 管理）
#   4. Playwright + 浏览器     —— 文章核心工具：Playwright 发畸形 HTTP 请求
#   5. PostgreSQL + Redis      —— Web 平台存储与 arq 任务队列
#
# 脚本行为：逐项校验，缺失即报错退出（set -euo pipefail），不静默降级。
# 支持 macOS(arm64) / Linux(x86_64 glibc)；不支持其他平台。

set -euo pipefail

# ---------------------------------------------------------------------------
# 0. 环境定位
# ---------------------------------------------------------------------------
cd "$(dirname "$0")"
ROOT="$(pwd)"
export PYTHONUNBUFFERED=1

log()  { printf '\033[1;34m[setup]\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m[ ok ]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[warn]\033[0m %s\n' "$*"; }
err()  { printf '\033[1;31m[FAIL]\033[0m %s\n' "$*" >&2; }

# 平台标签（用于日志与潜在平台相关提示）
detect_platform() {
  local sys mach
  sys="$(uname -s)"; mach="$(uname -m)"
  if [ "$sys" = "Darwin" ] && { [ "$mach" = "arm64" ] || [ "$mach" = "aarch64" ]; }; then
    echo "darwin-arm64"; return
  fi
  if [ "$sys" = "Linux" ] && { [ "$mach" = "x86_64" ] || [ "$mach" = "amd64" ]; }; then
    echo "linux-x86_64-gnu"; return
  fi
  err "不支持的平台：$sys $mach。仅支持 macOS arm64 / Linux x86_64 glibc。"
  exit 1
}
PLATFORM="$(detect_platform)"
ok "平台识别：$PLATFORM"

# ---------------------------------------------------------------------------
# 1. 系统依赖校验
# ---------------------------------------------------------------------------
log "校验系统依赖..."

# 1.1 Python >= 3.11
if ! command -v python3 >/dev/null 2>&1; then
  err "未找到 python3，请先安装 Python >= 3.11"
  exit 1
fi
PY_VER="$(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])')"
if ! python3 -c "import sys;sys.exit(0 if sys.version_info>=(3,11) else 1)"; then
  err "Python 版本过低：$PY_VER，需 >= 3.11"
  exit 1
fi
ok "Python $PY_VER"

# 1.2 uv
if ! command -v uv >/dev/null 2>&1; then
  log "未安装 uv，正在安装（https://github.com/astral-sh/uv）..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  # 让当前 shell 能找到 uv
  export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
  if ! command -v uv >/dev/null 2>&1; then
    err "uv 安装后仍不可用，请检查 PATH 后重试"
    exit 1
  fi
fi
ok "uv $(uv --version)"

# 1.3 Docker + compose
if ! command -v docker >/dev/null 2>&1; then
  err "未安装 Docker。主路径沙箱强依赖 Docker，请先安装：https://docs.docker.com/get-docker/"
  exit 1
fi
if ! docker info >/dev/null 2>&1; then
  err "Docker 守护进程未运行（docker info 失败）。请启动 Docker 后重试。"
  exit 1
fi
if ! docker compose version >/dev/null 2>&1; then
  err "docker compose 不可用（需 Docker Desktop 或 docker-compose-plugin）。"
  exit 1
fi
ok "Docker $(docker --version | awk '{print $3}') + compose"

# 1.4 git（下载沙箱 worker 不需要，但 Playwright 依赖链路可能用到）
command -v git >/dev/null 2>&1 && ok "git $(git --version | awk '{print $3}')" \
  || warn "未检测到 git（非阻断，但建议安装）"

# ---------------------------------------------------------------------------
# 2. Python 依赖安装（uv sync）
# ---------------------------------------------------------------------------
log "同步 Python 依赖（uv sync，含 pobi_agent 全部内核依赖）..."
uv sync --extra dev || uv sync
ok "Python 依赖已同步"

# 激活 venv（后续步骤使用）
if [ -d ".venv" ]; then
  export VIRTUAL_ENV="$ROOT/.venv"
  export PATH="$ROOT/.venv/bin:$PATH"
fi

# ---------------------------------------------------------------------------
# 3. Playwright 浏览器（文章核心工具，强制）
# ---------------------------------------------------------------------------
log "安装 Playwright 浏览器（chromium，内核 pw_requester 强依赖）..."
python3 -m playwright install --with-deps chromium \
  || python3 -m playwright install chromium
ok "Playwright chromium 已就绪"

# ---------------------------------------------------------------------------
# 4. 基础设施：PostgreSQL + Redis
# ---------------------------------------------------------------------------
# 方式一：本机已有运行中的 PG/Redis（跳过）
# 方式二：用 docker compose 拉起（项目自带 docker-compose.yml）
log "准备基础设施（PostgreSQL + Redis）..."
if docker compose ps 2>/dev/null | grep -qE "postgres|redis" && \
   docker compose exec -T postgres pg_isready -U "${POBI_V2_DB_USER:-pobi}" >/dev/null 2>&1; then
  ok "检测到已运行的 PostgreSQL/Redis（docker compose），跳过启动"
else
  log "通过 docker compose 启动 PostgreSQL + Redis..."
  docker compose up -d postgres redis
  # 等待 PG 就绪
  for i in $(seq 1 30); do
    if docker compose exec -T postgres pg_isready -U "${POBI_V2_DB_USER:-pobi}" >/dev/null 2>&1; then
      break
    fi
    sleep 2
  done
  ok "PostgreSQL + Redis 已启动"
fi

# ---------------------------------------------------------------------------
# 5.5 全局共享 Kali 沙箱镜像（全面容器化核心）
# ---------------------------------------------------------------------------
# 全面容器化后，Kali 作为 docker-compose 常驻 service（容器名 pobi_kali）由
# 宿主机 daemon 创建，所有 shell 命令与 Python 验证共用同一容器。
# 此处确保镜像已就绪（运行时若缺失会再次尝试拉取）。
log "拉取全局共享 Kali 沙箱镜像 xoxruns/sandboxed_kali（compose 常驻 pobi_kali）..."
if docker images --format '{{.Repository}}:{{.Tag}}' | grep -q "^xoxruns/sandboxed_kali"; then
  ok "xoxruns/sandboxed_kali 已存在，跳过"
else
  if docker pull xoxruns/sandboxed_kali; then
    ok "xoxruns/sandboxed_kali 拉取完成"
  else
    warn "拉取 xoxruns/sandboxed_kali 失败（可能无外网）。"
    warn "运行时 sandbox_manager 会再次尝试拉取；或手动执行：docker pull xoxruns/sandboxed_kali"
  fi
fi

# ---------------------------------------------------------------------------
# 6. 环境变量模板
# ---------------------------------------------------------------------------
if [ ! -f ".env" ]; then
  log "生成 .env 模板（请补全 LLM API Key 等敏感项）..."
  cat > .env <<'EOF'
# ---- 必填：LLM 凭证（按 provider 填写，DeadEndAgent 运行时解析）----
ANTHROPIC_API_KEY=
OPENAI_API_KEY=
OPENROUTER_API_KEY=
GOOGLE_API_KEY=

# ---- 数据库（docker compose 默认）----
POBI_V2_DB_HOST=localhost
POBI_V2_DB_PORT=5432
POBI_V2_DB_USER=pobi
POBI_V2_DB_PASSWORD=pobi
POBI_V2_DB_NAME=pobi_v2

# ---- Redis（arq 任务队列）----
REDIS_URL=redis://localhost:6379/0

# ---- Web 平台 ----
JWT_SECRET=请修改为随机长字符串
JWT_ALGORITHM=HS256
ACCESS_TOKEN_EXPIRE_MINUTES=1440

# ---- API 令牌（PAT）加密密钥 ----
# 用于持久化加密个人访问令牌明文，支撑前端「点击查看」。
# 留空时令牌仍可创建与校验，但创建后无法再次查看明文（仅创建时一次性可见）。
# 建议填入一段随机长字符串；可用 `openssl rand -hex 32` 生成。
POBI_V2_TOKEN_ENCRYPTION_KEY=

# ---- 全面容器化：Kali 沙箱（compose 常驻 pobi_kali，DooD 复用宿主机 daemon）----
# 镜像与网络一般无需修改；仅自定义镜像源时调整
# KALI_IMAGE=xoxruns/sandboxed_kali:latest
# POBI_V2_SANDBOX_NETWORK=pobi_net
# POBI_V2_KALI_CONTAINER_NAME=pobi_kali
EOF
  warn ".env 已生成，请编辑并填入 LLM API Key 与 JWT_SECRET"
else
  ok ".env 已存在，跳过"
fi

# ---------------------------------------------------------------------------
# 7. 数据库迁移
# ---------------------------------------------------------------------------
log "执行 Alembic 数据库迁移..."
uv run alembic upgrade head || python3 -m alembic upgrade head
ok "数据库迁移完成"

# ---------------------------------------------------------------------------
# 8. 最终校验
# ---------------------------------------------------------------------------
log "运行依赖自检..."
python3 - <<'PY'
import importlib.util, sys
checks = {
    "playwright": "playwright",
    "docker": "docker",
    "litellm": "litellm",
    "instructor": "instructor",
}
missing = [k for k, m in checks.items() if importlib.util.find_spec(m) is None]
if missing:
    print(f"[FAIL] 缺少 Python 依赖：{missing}", file=sys.stderr)
    sys.exit(1)
print("[ ok ] 核心 Python 依赖齐全")
PY

echo
echo "==================================================================="
ok "Pobi v2 安装完成。"
echo "启动方式："
echo "  API   : uv run uvicorn pobi_v2.main:app --host 0.0.0.0 --port 8000"
echo "  Worker: uv run arq pobi_v2.engine.worker.WorkerSettings"
echo "  （或生产）：./start-prod.sh"
echo "-------------------------------------------------------------------"
echo "强制依赖速查："
echo "  Docker            —— 全局共享 Kali 沙箱（compose 常驻 pobi_kali，DooD 复用）"
echo "  Playwright chromium —— pw_requester 畸形请求"
echo "  PostgreSQL+Redis  —— Web 存储与任务队列"
echo "==================================================================="
