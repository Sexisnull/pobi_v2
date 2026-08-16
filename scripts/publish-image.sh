#!/usr/bin/env bash
# Pobi v2 全量镜像一键发布到阿里云 ACR 个人版
# ---------------------------------------------------------------------------
# 用法：
#   ./scripts/publish-image.sh            # 推到公网地址（默认）
#   ./scripts/publish-image.sh --vpc      # 推到 VPC 内网地址（ECS 同地域更快）
#   ./scripts/publish-image.sh --no-login # 跳过 docker login（已登录过）
#   ./scripts/publish-image.sh --tag 1.1.0
#
# 镜像命名：<registry>/pobi/pobi_v2:<tag>
#   公网：crpi-9ztkvcvf31ip1h4c.cn-hangzhou.personal.cr.aliyuncs.com/pobi/pobi_v2
#   VPC ：crpi-9ztkvcvf31ip1h4c-vpc.cn-hangzhou.personal.cr.aliyuncs.com/pobi/pobi_v2
#
# 凭证：优先读 .env 的 ACR_USERNAME / ACR_PASSWORD；缺失则交互提示手动 login。
# 本地 tag 默认与 version.txt 一致；可 --tag 覆盖。
# ---------------------------------------------------------------------------
set -euo pipefail

cd "$(dirname "$0")/.."

# ---- 参数解析 ----
USE_VPC=0
NO_LOGIN=0
TAG_OVERRIDE=""
while [ $# -gt 0 ]; do
  case "$1" in
    --vpc)      USE_VPC=1 ;;
    --no-login) NO_LOGIN=1 ;;
    --tag)      TAG_OVERRIDE="${2:-}"; shift ;;
    -h|--help)
      grep -E '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "未知参数: $1"; exit 1 ;;
  esac
  shift
done

# ---- 仓库地址 ----
if [ "$USE_VPC" -eq 1 ]; then
  REGISTRY="crpi-9ztkvcvf31ip1h4c-vpc.cn-hangzhou.personal.cr.aliyuncs.com"
else
  REGISTRY="crpi-9ztkvcvf31ip1h4c.cn-hangzhou.personal.cr.aliyuncs.com"
fi
REPO="${REGISTRY}/pobi/pobi_v2"

# ---- tag 来源 ----
if [ -n "$TAG_OVERRIDE" ]; then
  TAG="$TAG_OVERRIDE"
elif [ -f version.txt ]; then
  TAG="$(tr -d '[:space:]' < version.txt)"
fi
[ -z "${TAG:-}" ] && { echo "无法确定 tag（version.txt 为空且未传 --tag）"; exit 1; }

LOCAL_IMAGE="pobi_v2:${TAG}"
REMOTE_IMAGE="${REPO}:${TAG}"

echo "==> 目标镜像：${REMOTE_IMAGE}"

# ---- 登录 ----
if [ "$NO_LOGIN" -eq 0 ]; then
  if [ -f .env ]; then
    set -a; . ./.env; set +a
  fi
  if [ -n "${ACR_USERNAME:-}" ] && [ -n "${ACR_PASSWORD:-}" ]; then
    echo "==> 使用 .env 凭证登录 ${REGISTRY}"
    echo "${ACR_PASSWORD}" | docker login --username="${ACR_USERNAME}" "${REGISTRY}" --password-stdin
  else
    echo "==> 未在 .env 找到 ACR_USERNAME/ACR_PASSWORD，请手动登录："
    echo "    docker login --username=<阿里云账号> ${REGISTRY}"
    docker login "${REGISTRY}"
  fi
fi

# ---- 构建本地镜像（全量，含 Playwright 浏览器等）----
echo "==> 构建全量镜像 ${LOCAL_IMAGE}"
docker compose build api worker

# ---- tag + push ----
echo "==> 标记并推送 ${REMOTE_IMAGE}"
docker tag "${LOCAL_IMAGE}" "${REMOTE_IMAGE}"
docker push "${REMOTE_IMAGE}"

echo ""
echo "==> 发布完成：${REMOTE_IMAGE}"
echo "    他人部署只需在 .env 设置："
echo "    POBI_IMAGE=${REMOTE_IMAGE}"
echo "    然后执行 ./start-prod.sh 即可（优先 pull，无需本地构建）"
