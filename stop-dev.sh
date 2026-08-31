#!/usr/bin/env bash
# Pobi v2 开发模式停止脚本
# 结束 docker compose 后端服务，并清理后台的 Vite dev server 进程。
set -euo pipefail

cd "$(dirname "$0")"

echo "==> [dev] 停止 Docker 后端服务（api/worker/postgres/redis/kali）"
# 仅停止 compose 管理的服务，不删除卷；web 默认未启动（profiles），无影响。
docker compose down

# 清理后台 Vite dev server
VITE_PID_FILE="/tmp/pobi_vite_dev.pid"
if [ -f "$VITE_PID_FILE" ]; then
  VITE_PID="$(cat "$VITE_PID_FILE" 2>/dev/null || true)"
  if [ -n "${VITE_PID:-}" ] && kill -0 "$VITE_PID" 2>/dev/null; then
    echo "==> [dev] 结束 Vite dev server (pid=$VITE_PID)"
    kill "$VITE_PID" 2>/dev/null || true
  fi
  rm -f "$VITE_PID_FILE"
fi

# 兜底：若 pid 文件丢失但 5173 仍被占用，按端口结束
if command -v lsof >/dev/null 2>&1; then
  VITE_PORT_PID="$(lsof -ti tcp:5173 2>/dev/null || true)"
  if [ -n "${VITE_PORT_PID:-}" ]; then
    echo "==> [dev] 按端口清理残留 Vite 进程 (pid=$VITE_PORT_PID)"
    kill "$VITE_PORT_PID" 2>/dev/null || true
  fi
fi

echo "==> [dev] 已停止。前端如需再次启动，执行 ./start-dev.sh"
