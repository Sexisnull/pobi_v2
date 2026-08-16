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

echo "==> [dev] 启动服务（源码挂载 + 热重载）"
docker compose up -d

echo ""
echo "==> 开发模式已就绪。"
echo "    前端入口：      http://127.0.0.1/"
echo "    后端直连：      http://127.0.0.1:8000/health"
echo "    日志（跟随）：  docker compose logs -f api worker"
echo ""
echo "    提示：修改 api（pobi_v2/）源码后 uvicorn --reload 自动生效；"
echo "          修改 worker（pobi_agent/）源码后需手动 docker compose restart worker 生效。"
echo "    若修改了依赖或 Dockerfile，请执行： ./start-dev.sh rebuild"
