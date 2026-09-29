#!/usr/bin/env bash
# =============================================================================
# QHYC 数据库备份脚本（评估文档 §8.3 / P1#11：数据库备份脚本 + crontab）
#
# 每日凌晨备份，保留 7 天。建议加入 crontab：
#   0 4 * * * /Docker/qhyc/scripts/ops/backup_db.sh >> /var/log/qhyc_backup.log 2>&1
#
# 说明：直接在 timescaledb 容器内执行 pg_dump，避免宿主机是否安装 psql 的问题。
# 若容器本地 socket 为 trust，可不设 PGPASSWORD；否则请在环境中导出：
#   export PGPASSWORD='<云端数据库密码>'
# =============================================================================
set -euo pipefail

CONTAINER="${QHYC_DB_CONTAINER:-qhyc-timescaledb}"
PGUSER="${PGUSER:-futures}"
PGDB="${PGDB:-futures}"
BACKUP_DIR="${BACKUP_DIR:-/backup}"
DATE="$(date +%Y%m%d_%H%M%S)"
OUT="${BACKUP_DIR}/futures_${DATE}.sql.gz"

mkdir -p "$BACKUP_DIR"

if [ -n "${PGPASSWORD:-}" ]; then
  docker exec -e PGPASSWORD="$PGPASSWORD" "$CONTAINER" \
    pg_dump -U "$PGUSER" "$PGDB" | gzip > "$OUT"
else
  docker exec "$CONTAINER" \
    pg_dump -U "$PGUSER" "$PGDB" | gzip > "$OUT"
fi

# 保留最近 7 天
find "$BACKUP_DIR" -name "futures_*.sql.gz" -mtime +7 -delete

echo "[backup] wrote $OUT ($(du -h "$OUT" | cut -f1))"
