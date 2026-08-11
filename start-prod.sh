#!/usr/bin/env bash
# Pobi v2 生产部署脚本
# 前置：.env 已配置（ACR_REGISTRY / 数据库 / JWT_SECRET 等）；
#       最新镜像已构建并推送至 ACR（见 README「发布流程」）。
set -euo pipefail

cd "$(dirname "$0")"

echo "==> 拉取/构建最新镜像"
docker compose build

echo "==> 启动依赖与全部服务"
docker compose up -d

echo "==> 等待数据库就绪并执行迁移"
# 简单等待 postgres 健康
for i in $(seq 1 30); do
  if docker compose exec -T postgres pg_isready -U "${POBI_V2_DB_USER:-pobi}" >/dev/null 2>&1; then
    break
  fi
  sleep 2
done

docker compose exec -T api alembic upgrade head

echo "==> 部署完成。访问入口：http://<host>/"
echo "    后端健康检查：http://<host>/api/v1/health"
