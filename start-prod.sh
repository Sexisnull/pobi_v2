#!/usr/bin/env bash
# Pobi v2 一键本地构建部署脚本
# ---------------------------------------------------------------------------
# 任何人 git clone 本仓库后，只需：
#   1. 复制 .env.example 为 .env（或脚本自动生成）
#   2. 填入至少一个 LLM API Key
#   3. 执行 ./start-prod.sh
# 即可：构建全部镜像 -> 启动所有 docker 服务（统一内部网络 pobi_net）
#      -> 端口映射（web 80 给用户用；postgres/redis 仅内部直连）
#      -> 等待全局 Kali 沙箱就绪 -> 数据库迁移。
#
# 说明：Kali 沙箱「对外访问」指容器内 agent 主动向外发起扫描/请求（出站），
#       经宿主网络出口与 host.docker.internal 回连，无需暴露入站端口。
# ---------------------------------------------------------------------------
set -euo pipefail

cd "$(dirname "$0")"

# ── 1. .env 自动生成 ──────────────────────────────────────────────────────
if [ ! -f ".env" ]; then
  if [ -f ".env.example" ]; then
    echo "==> 未检测到 .env，基于 .env.example 生成"
    cp .env.example .env
  else
    echo "==> 未检测到 .env / .env.example，创建最小 .env"
    cat > .env <<'EOF'
POBI_V2_DB_HOST=db
POBI_V2_DB_PORT=5432
POBI_V2_DB_USER=pobi
POBI_V2_DB_PASSWORD=pobi
POBI_V2_DB_NAME=pobi_v2
REDIS_URL=redis://redis:6379/0
JWT_ALGORITHM=HS256
ACCESS_TOKEN_EXPIRE_MINUTES=1440
POBI_V2_API_BASE=http://localhost/api/v1
KALI_IMAGE=xoxruns/sandboxed_kali:latest
POBI_V2_SANDBOX_NETWORK=pobi_net
POBI_V2_KALI_CONTAINER_NAME=pobi_kali
EOF
  fi
fi

# 加载 .env 以便脚本内读取并补全密钥
set -a
# shellcheck disable=SC1091
. ./.env
set +a

# 为空白密钥自动生成随机值（保证 JWT 与 PAT 加密可用）
if [ -z "${JWT_SECRET:-}" ]; then
  JWT_SECRET="$(openssl rand -hex 32)"
  echo "JWT_SECRET=${JWT_SECRET}" >> .env
  echo "    [auto] 已生成随机 JWT_SECRET"
fi
if [ -z "${POBI_V2_TOKEN_ENCRYPTION_KEY:-}" ]; then
  POBI_V2_TOKEN_ENCRYPTION_KEY="$(openssl rand -hex 32)"
  echo "POBI_V2_TOKEN_ENCRYPTION_KEY=${POBI_V2_TOKEN_ENCRYPTION_KEY}" >> .env
  echo "    [auto] 已生成随机 POBI_V2_TOKEN_ENCRYPTION_KEY（支持 PAT 点击查看）"
fi

# LLM Key 缺失提醒（不阻断，便于仅做部署验证）
if [ -z "${ANTHROPIC_API_KEY:-}" ] && [ -z "${OPENAI_API_KEY:-}" ] \
   && [ -z "${OPENROUTER_API_KEY:-}" ] && [ -z "${GOOGLE_API_KEY:-}" ]; then
  echo "    [warn] 未配置任何 LLM API Key，agent 运行时会失败；请在 .env 中至少填入一个。"
fi

# ── 2. 准备 pobi_v2 镜像（远端 pull 优先，本地 build 兜底）──────────────────
# 注：compose 中 service 名为 api / worker（共享 pobi_v2 镜像），无 pobi_v2 service。
# POBI_IMAGE 留空 → 本地 build api worker；
# 已设置 → 优先 `docker compose pull` 这两个 service 的镜像，失败回退本地 build。
  if [ -n "${POBI_IMAGE:-}" ]; then
  echo "==> 检测到 POBI_IMAGE=${POBI_IMAGE}，优先拉取远端镜像"
  if docker compose -f docker-compose.yml pull api worker 2>/dev/null; then
    echo "    远端镜像拉取成功，跳过本地构建"
  else
    echo "    [warn] 远端镜像拉取失败，回退本地构建 api worker"
    docker compose -f docker-compose.yml build api worker
  fi
else
  echo "==> 本地构建 pobi_v2 镜像（api worker）"
  docker compose -f docker-compose.yml build api worker
fi

# ── 3. 启动所有服务 ───────────────────────────────────────────────────────
echo "==> 启动全部服务（统一内部网络 pobi_net；web 端口 80 对外）"
# 显式 -f docker-compose.yml，隔离 docker-compose.override.yml（dev 配置：
# 源码挂载 / uvicorn --reload / restart:no），防止生产误用开发配置。
docker compose -f docker-compose.yml up -d

# ── 4. 等待全局共享 Kali 沙箱（pobi_kali）就绪 ────────────────────────────
echo "==> 等待全局 Kali 沙箱就绪"
for i in $(seq 1 30); do
  if docker compose -f docker-compose.yml ps kali 2>/dev/null | grep -qE "healthy|running"; then
    echo "    Kali 沙箱已就绪"
    break
  fi
  sleep 2
done

# ── 5. 等待数据库就绪并执行迁移 ───────────────────────────────────────────
echo "==> 等待数据库并应用迁移"
for i in $(seq 1 30); do
  if docker compose -f docker-compose.yml exec -T postgres pg_isready -U "${POBI_V2_DB_USER:-pobi}" >/dev/null 2>&1; then
    break
  fi
  sleep 2
done

docker compose -f docker-compose.yml exec -T api alembic upgrade head

# ── 5.5 解析首次启动自动 seed 的 admin 凭证（供用户登录）──────────────────
# 应用仅在库内无用户时自动创建 admin；凭证可被 .env 的 POBI_V2_ADMIN_* 覆盖。
ADMIN_EMAIL="${POBI_V2_ADMIN_EMAIL:-admin@example.com}"
ADMIN_PASS="${POBI_V2_ADMIN_PASSWORD:-admin123456}"
# 若 .env 未显式设置，则回写，保证 .env 与实际 seed 值一致、便于查阅
if [ -z "${POBI_V2_ADMIN_EMAIL:-}" ]; then
  echo "POBI_V2_ADMIN_EMAIL=${ADMIN_EMAIL}" >> .env
fi
if [ -z "${POBI_V2_ADMIN_PASSWORD:-}" ]; then
  echo "POBI_V2_ADMIN_PASSWORD=${ADMIN_PASS}" >> .env
fi

# ── 6. 完成 ───────────────────────────────────────────────────────────────
echo ""
echo "==> 部署完成。"
echo "    前端入口：      http://<host>/"
echo "    后端健康检查：  http://<host>/api/v1/health"
echo ""
echo "    默认管理员账号（首次启动自动创建，库内已有用户则不会重建）："
echo "        邮箱：  ${ADMIN_EMAIL}"
echo "        密码：  ${ADMIN_PASS}"
echo "        登录：  http://<host>/  →  使用上方账号登录"
echo ""
echo "    Kali 沙箱交互：  docker compose -f docker-compose.yml exec -it kali /bin/bash"
echo "    查看日志：      docker compose -f docker-compose.yml logs -f api worker"
