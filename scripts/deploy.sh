#!/usr/bin/env bash
# =============================================================================
# qhyc 一键部署脚本（Phase 6）
#
# 设计前提（用户 2026-09-29 拍板）
#   * 云端为主：云端跑全部采集/计算/调度；本地**只连本地库**，不连云端库；
#   * 本地通过 cloud_local_sync（云→地）同步，CLOUD_SYNC_ENABLED=1 生效；
#   * 全部代码在 git 内 → 换机器只需 clone + 本脚本。
#
# 用法
#   bash scripts/deploy.sh local          # 本地（默认 docker-compose.yml）
#   bash scripts/deploy.sh cloud          # 云端（docker-compose.cloud.yml）
#   bash scripts/deploy.sh local --no-build   # 跳过镜像构建（只换代码+重启）
#   SKIP_MIGRATE=1 bash scripts/deploy.sh cloud
#
# 步骤
#   1. git pull --ff-only           （代码是唯一真源）
#   2. docker compose build         （前端 dist 在镜像内构建 → 单一来源）
#   3. docker compose up -d         （滚动重建容器）
#   4. 容器内执行 migrations/*.sql   （幂等，记录到 schema_migrations）
#   5. 重启 scheduler + api，并打印启动自检日志（[scheduler][ASSET]）
# =============================================================================
set -euo pipefail

TARGET="${1:-local}"
NO_BUILD=0
for arg in "$@"; do
  [ "$arg" = "--no-build" ] && NO_BUILD=1
done

case "$TARGET" in
  local) COMPOSE_FILE="docker-compose.yml";  SCHED="qhyc-scheduler"; API="qhyc-api" ;;
  cloud) COMPOSE_FILE="docker-compose.cloud.yml"; SCHED="qhyc-scheduler"; API="qhyc-api" ;;
  *) echo "用法: bash scripts/deploy.sh {local|cloud} [--no-build]"; exit 2 ;;
esac

cd "$(dirname "$0")/.."
echo "=== [1/5] git pull (ff-only) ==="
git pull --ff-only || { echo "❌ git pull 失败（有本地提交或冲突？）"; exit 1; }

echo "=== [2/5] build image (${COMPOSE_FILE}) ==="
if [ "$NO_BUILD" = "1" ]; then
  echo "    --no-build：跳过"
else
  docker compose -f "$COMPOSE_FILE" build
fi

echo "=== [3/5] up -d ==="
docker compose -f "$COMPOSE_FILE" up -d

echo "=== [4/5] apply migrations ==="
if [ "${SKIP_MIGRATE:-0}" = "1" ]; then
  echo "    SKIP_MIGRATE=1：跳过"
else
  # 云端：容器内 POSTGRES_HOST=timescaledb（云端库）；本地：同理连本地库
  docker exec "$SCHED" python /app/scripts/db_apply_migrations.py --apply || {
    echo "❌ 迁移失败，请查看上方输出"; exit 1; }
fi

echo "=== [5/5] restart + 启动自检 ==="
docker restart "$SCHED" "$API" >/dev/null
sleep 8
docker logs "$SCHED" --tail 20 2>&1 | grep -E "ASSET|registered|ERROR|Traceback" || true
docker logs "$API"   --tail 10 2>&1 | grep -E "ERROR|Traceback|Uvicorn running" || true

echo
echo "✅ 部署完成（$TARGET）。验收："
echo "   1) 调度日志应见 [scheduler][ASSET] 生产脚本自检通过"
echo "   2) 云端实例应注册 16:20 factor_v1v6 / 16:45 factor_ic_monitor"
echo "   3) 本地实例应见 SYNC-ONLY mode: registered cloud_local_sync"
