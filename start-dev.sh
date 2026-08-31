#!/usr/bin/env bash
# Pobi v2 开发模式一键启动
# ---------------------------------------------------------------------------
# 与 start-prod.sh 的区别：
#   - 加载 docker-compose.override.yml（Docker 默认自动加载，无需 -f 指定）
#   - api/worker 挂载宿主机源码，改完即时生效（无需重建镜像）
#   - api 启用 uvicorn --reload，文件变化自动重启
#   - worker 不使用 arq --watch（--watch 的 SIGUSR1 会让运行中任务被 cancel 且重载后
#     卡在 avfs 挂载造成假活离线）；改 worker 源码后请手动 docker compose restart worker
#   - 不自动重建镜像（依赖层 /opt/venv 复用，首次需 build 一次）
#
# 用法：
#   ./start-dev.sh            # 首次会 build 一次，之后直接 up
#   ./start-dev.sh rebuild    # 强制重建镜像（改了依赖/ Dockerfile 时）
# ---------------------------------------------------------------------------
set -euo pipefail

cd "$(dirname "$0")"

# 加载 .env（dev 也复用同一份配置）
if [ -f ".env" ]; then
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
fi

REBUILD="${1:-}"

if [ -n "${POBI_IMAGE:-}" ]; then
  echo "==> [dev] 检测到 POBI_IMAGE 已设置，开发模式忽略远端镜像，走本地构建"
  unset POBI_IMAGE
fi

if [ "${REBUILD}" = "rebuild" ]; then
  echo "==> [dev] 强制重建 api/worker 镜像"
  docker compose build api worker
else
  # 首次若镜像不存在则构建，存在则直接复用（保留 /opt/venv 依赖层）
  if ! docker compose images api 2>/dev/null | grep -q "pobi_v2"; then
    echo "==> [dev] 未检测到本地镜像，首次构建 api/worker"
    docker compose build api worker
  fi
fi

echo "==> [dev] 启动后端服务（源码挂载 + 热重载，不含 web 容器）"
docker compose up -d

# ---- 前端：宿主 Vite dev server（HMR，无需 build / 不进 Docker）----
# 开发阶段前端不依赖 Docker 的 web 容器，直接在宿主起 Vite（端口 5173），
# 其 /api 代理指向 127.0.0.1:8000（宿主映射的 api 容器），改完即时生效。
WEBAPP_DIR="$(pwd)/webapp"
VITE_PID=""
if [ -d "$WEBAPP_DIR" ] && command -v npm >/dev/null 2>&1; then
  if [ -d "$WEBAPP_DIR/node_modules" ]; then
    echo "==> [dev] 后台启动前端 Vite dev server（http://127.0.0.1:5173）"
    ( cd "$WEBAPP_DIR" && nohup npm run dev > /tmp/pobi_vite_dev.log 2>&1 & echo $! > /tmp/pobi_vite_dev.pid )
    VITE_PID="$(cat /tmp/pobi_vite_dev.pid 2>/dev/null || true)"
  else
    echo "    [warn] webapp/node_modules 不存在，跳过自动启动 Vite。"
    echo "           请先执行： cd webapp && npm install && npm run dev"
  fi
else
  echo "    [warn] 未检测到 webapp/ 或 npm，前端请手动启动：cd webapp && npm run dev"
fi

echo ""
echo "==> 开发模式已就绪。"
echo "    前端入口(HMR) : http://127.0.0.1:5173   （Vite 自动热更新，改完即生效）"
echo "    后端直连：      http://127.0.0.1:8000/health"
echo "    前端日志：      tail -f /tmp/pobi_vite_dev.log"
echo "    Docker 日志：   docker compose logs -f api worker"
echo ""
echo "    提示：修改前端（webapp/）后 Vite HMR 自动生效，无需 build、无需重启 Docker；"
echo "          修改 api（pobi_v2/）源码后 uvicorn --reload 自动生效；"
echo "          修改 worker（pobi_agent/）源码后需手动 docker compose restart worker 生效。"
echo "          若修改了依赖或 Dockerfile，请执行： ./start-dev.sh rebuild"
echo ""
echo "    停止：./stop-dev.sh   （会一并结束 Vite 进程）"
