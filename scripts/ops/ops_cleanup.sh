#!/usr/bin/env bash
# qhyc 运维：日志与产物清理（云端轻量服务器）
#
# 职责：
#   1. 操作系统日志：journald 保留 7 天 / 上限 300M；清理 /var/log 下轮转残件
#   2. Docker：清理容器 json 日志（daemon 已配 50m×3，此处兜底）与悬空镜像/构建缓存
#   3. qhyc 应用日志：logs 卷内 *.log 超过 7 天删除
#   4. 简报与过程文件：/app/qh（qh_brief 卷）下 7 天前的文件删除
#      —— 但「持仓情况和手续费」是账户/费率状态，**永不清理**
#
# 用法：sudo bash ops_cleanup.sh [--dry-run] [--days N]
# cron：20 3 * * * /bin/bash /opt/qhyc-ops/ops_cleanup.sh >> /var/log/qhyc_ops_cleanup.log 2>&1

set -uo pipefail

DAYS="${DAYS:-7}"
DRY=0
for a in "$@"; do
  case "$a" in
    --dry-run) DRY=1 ;;
    --days) shift; DAYS="${1:-7}" ;;
  esac
done

BRIEF_ROOT="/var/lib/docker/volumes/qhyc_qh_brief/_data"
LOGS_ROOT="/var/lib/docker/volumes/qhyc_logs/_data"
RUNTIME_ROOT="/var/lib/docker/volumes/qhyc_runtime/_data"
KEEP_DIR="持仓情况和手续费"   # 账户/费率状态，禁止清理

log() { echo "[$(date '+%F %T')] $*"; }
run() { if [ "$DRY" -eq 1 ]; then echo "    [dry-run] $*"; else eval "$@"; fi; }

log "=== 清理开始（保留 ${DAYS} 天，dry-run=${DRY}）==="

# ---------- 1. 操作系统日志 ----------
log "[1] 系统日志"
run "journalctl --vacuum-time=${DAYS}d --vacuum-size=300M >/dev/null 2>&1"
# 轮转残件：.gz / .1 / 旧日期日志
run "find /var/log -type f -mtime +${DAYS} \\( -name '*.gz' -o -name '*.[0-9]' -o -name '*.old' \\) -delete 2>/dev/null"
# 运维脚本自身日志轮转（由 logrotate 管，这里兜底）
run "find /var/log -maxdepth 1 -type f -name 'qhyc_ops_*.log' -size +20M -exec truncate -s 0 {} \; 2>/dev/null"

# ---------- 2. Docker ----------
log "[2] Docker 日志与悬空资源"
if command -v docker >/dev/null 2>&1; then
  # 容器 json 日志（daemon.json 已配 50m×3；此处清理非 qhyc 容器与异常膨胀）
  run "find /var/lib/docker/containers -name '*.log' -type f -size +200M -exec truncate -s 100M {} \; 2>/dev/null"
  run "docker image prune -f --filter 'until=$((DAYS*24))h' >/dev/null 2>&1"
  run "docker builder prune -f --filter 'until=$((DAYS*24))h' >/dev/null 2>&1"
fi

# ---------- 3. qhyc 应用日志 ----------
log "[3] qhyc 应用日志（logs / runtime 卷）"
for d in "$LOGS_ROOT" "$RUNTIME_ROOT"; do
  [ -d "$d" ] || continue
  run "find '$d' -type f -name '*.log' -mtime +${DAYS} -delete 2>/dev/null"
  run "find '$d' -type f -name '*.log' -size +200M -exec truncate -s 100M {} \; 2>/dev/null"
done

# ---------- 4. 简报与过程文件 ----------
log "[4] 简报与过程文件（保留 ${DAYS} 天，跳过 ${KEEP_DIR}）"
if [ -d "$BRIEF_ROOT" ]; then
  # 4.1 过程思考（含 logs 子目录）：全部按天清理
  run "find '$BRIEF_ROOT/过程思考' -type f -mtime +${DAYS} -delete 2>/dev/null"
  # 4.2 简报正文：按天清理
  run "find '$BRIEF_ROOT/简报内容' -type f -mtime +${DAYS} -delete 2>/dev/null"
  # 4.3 其余顶层文件（排除账户/费率目录）
  run "find '$BRIEF_ROOT' -maxdepth 1 -type f -mtime +${DAYS} -delete 2>/dev/null"
  # 4.4 空目录清理（不动 KEEP_DIR）
  run "find '$BRIEF_ROOT' -mindepth 1 -type d -empty -not -path '*${KEEP_DIR}*' -delete 2>/dev/null"
  log "    简报目录剩余：$(find "$BRIEF_ROOT/简报内容" -type f 2>/dev/null | wc -l) 个文件"
fi

# ---------- 5. 磁盘水位 ----------
log "[5] 磁盘水位"
df -h / | tail -1
docker system df 2>/dev/null | head -3

log "=== 清理结束 ==="
