#!/bin/bash
# Weekly full pg_dump (custom format) — 容量受控、TOC 校验、状态可见。
#
# CL-20261112-06 P1：旧版按固定份数保留（retain-7，用 tail 截掉第 8 份起），
# 单份约 7.6G × 7 ≈ 53G，而 /dev/vda2 当时仅剩 34G —— 周备份会把系统盘撑爆，
# 且从未做过完整性校验、异地未配置也无人知晓。本版：
#   * 写前空间预检（峰值 ≈ 2× 上次 dump：容器内临时文件 + 宿主副本）；
#   * pg_restore --list 双重校验（容器内原件 + 宿主副本，后者走只读 bind mount，
#     零拷贝）；不通过则 ABORT，绝不把坏 dump 当备份；
#   * 按 BACKUP_MAX_TOTAL_GB 动态删最旧（至少留 1 份），不再是固定份数；
#   * 状态写 /app/runtime/backup_health.json 对应的宿主 bind mount，供
#     alerting._check_backup_capacity 与 OPS-BACKUP-CAPACITY-SAFE 读取；
#   * BACKUP_REMOTE_DIR 配置即异地复制，未配置则在状态里显式记 false。
#
# 生产 crontab（周日 04:30，由 task_runner 包装）指向宿主副本：
#   /home/ubuntu/qhyc_work/scripts/weekly_pgdump.sh
# 本文件是入库的可复现版本，改动后须同步到上述宿主路径。
#
# 用法：
#   ./weekly_pgdump.sh                 # 正式 dump
#   ./weekly_pgdump.sh --report-only  # 只算容量 + 校验现有最新 dump + 刷新状态
set -euo pipefail

D=${BACKUP_DIR:-$HOME/qhyc_work/backup/pgdump}
STATE=${BACKUP_HEALTH_FILE:-$HOME/qhyc_work/share/runtime/backup_health.json}
MAX_TOTAL_GB=${BACKUP_MAX_TOTAL_GB:-25}
MIN_FREE_GB=${BACKUP_MIN_FREE_GB:-20}
RESERVE_GB=${BACKUP_RESERVE_GB:-8}
REMOTE_DIR=${BACKUP_REMOTE_DIR:-}
IMG=$(docker inspect qhyc-timescaledb --format '{{.Config.Image}}' 2>/dev/null \
      || echo timescale/timescaledb:2.17.1-pg16)
mkdir -p "$D" "$(dirname "$STATE")"

bytes_of() { find "$D" -maxdepth 1 -name 'pgdump_*.dump' -printf '%s\n' 2>/dev/null \
             | awk '{s+=$1} END{print s+0}'; }
TOTAL_BYTES=$(bytes_of)
TOTAL_GB=$(awk -v b="$TOTAL_BYTES" 'BEGIN{printf "%.2f", b/1073741824}')
FREE_GB=$(df -BG --output=avail / | tail -1 | tr -dc '0-9')
newest_name() {
    local f
    f=$(ls -1t "$D"/pgdump_*.dump 2>/dev/null | head -1 || true)
    if [ -n "$f" ]; then basename "$f"; fi
}

# 只读 bind mount 校验 custom-format 目录，7.6G 零拷贝
toc_ok() {
    [ -n "${1:-}" ] || return 1
    timeout 300 docker run --rm -v "$D":/b:ro --entrypoint pg_restore "$IMG" \
        --list "/b/$1" >/dev/null 2>&1
}

write_state() {   # $1=toc_ok(true/false)  $2=note  $3=last_dump
    local remote=false
    [ -n "$REMOTE_DIR" ] && remote=true
    local t
    t=$(bytes_of)
    local tg fg
    tg=$(awk -v b="$t" 'BEGIN{printf "%.2f", b/1073741824}')
    fg=$(df -BG --output=avail / | tail -1 | tr -dc '0-9')
    cat > "$STATE.tmp" <<JSON
{
  "updated_at": "$(date -Is)",
  "total_gb": ${tg},
  "free_gb": ${fg},
  "max_total_gb": ${MAX_TOTAL_GB},
  "min_free_gb": ${MIN_FREE_GB},
  "remote_configured": ${remote},
  "last_restore_list_ok": ${1},
  "last_dump": "${3}",
  "copies": $(find "$D" -maxdepth 1 -name 'pgdump_*.dump' | wc -l),
  "note": "${2}"
}
JSON
    mv "$STATE.tmp" "$STATE"
}

# 按总量上限删最旧，至少保留 1 份
prune() {
    local total n oldest sz
    total=$(bytes_of)
    while :; do
        n=$(find "$D" -maxdepth 1 -name 'pgdump_*.dump' | wc -l)
        [ "$n" -le 1 ] && break
        awk -v b="$total" -v m="$MAX_TOTAL_GB" \
            'BEGIN{exit !(b/1073741824 > m)}' || break
        oldest=$(ls -1t "$D"/pgdump_*.dump | tail -1)
        sz=$(stat -c %s "$oldest")
        rm -f "$oldest"
        total=$((total - sz))
        echo "$(date) prune $(basename "$oldest") (${sz}B)"
    done
}

if [ "${1:-}" = "--report-only" ]; then
    n=$(newest_name)
    ok=false
    if toc_ok "$n"; then ok=true; fi
    write_state "$ok" "report-only TOC verification of newest dump" "$n"
    echo "$(date) report-only: total=${TOTAL_GB}G free=${FREE_GB}G toc_ok=${ok} newest=${n}"
    exit 0
fi

# ---- 写前空间预检：峰值 ≈ 2× 上次 dump ----
last=$(newest_name)
last_bytes=0
if [ -n "$last" ]; then last_bytes=$(stat -c %s "$D/$last"); fi
EST_GB=$(awk -v b="$last_bytes" 'BEGIN{printf "%.1f", b/1073741824*2}')
NEED=$(awk -v f="$FREE_GB" -v e="$EST_GB" -v r="$RESERVE_GB" \
       'BEGIN{print (f-e < r) ? 1 : 0}')
if [ "$NEED" = "1" ]; then
    echo "$(date) WARN free=${FREE_GB}G est=${EST_GB}G reserve=${RESERVE_GB}G → prune" >&2
    prune
    FREE_GB=$(df -BG --output=avail / | tail -1 | tr -dc '0-9')
    NEED=$(awk -v f="$FREE_GB" -v e="$EST_GB" -v r="$RESERVE_GB" \
           'BEGIN{print (f-e < r) ? 1 : 0}')
    if [ "$NEED" = "1" ]; then
        write_state false "ABORT 空间不足 free=${FREE_GB}G est=${EST_GB}G" "$last"
        echo "$(date) ABORT: 空间不足，未执行 pg_dump（DB 未受影响）" >&2
        exit 1
    fi
fi

TS=$(date +%Y%m%d_%H%M%S)
IN=/tmp/pgdump_$TS.dump
OUT=$D/pgdump_$TS.dump
NAME=$(basename "$OUT")

docker exec qhyc-timescaledb pg_dump -U futures -Fc -d futures -f "$IN"
# 容器内原件先校验 TOC，坏 dump 不复制到宿主
if ! docker exec qhyc-timescaledb pg_restore --list "$IN" >/dev/null 2>&1; then
    docker exec qhyc-timescaledb rm -f "$IN"
    write_state false "ABORT 容器内 dump TOC 校验失败" "$last"
    echo "$(date) ABORT: 容器内 dump TOC 校验失败" >&2
    exit 1
fi
docker cp "qhyc-timescaledb:$IN" "$OUT.part" >/dev/null
docker exec qhyc-timescaledb rm -f "$IN"
SZ=$(stat -c %s "$OUT.part")
if [ "$SZ" -lt 104857600 ]; then
    write_state false "ABORT dump too small ${SZ}B" "$last"
    echo "$(date) ABORT: dump too small ${SZ}B, keep .part for diagnosis" >&2
    exit 1
fi
mv "$OUT.part" "$OUT"
# 宿主副本再校验一次（docker cp 也可能截断）
if ! toc_ok "$NAME"; then
    write_state false "ABORT 宿主副本 TOC 校验失败" "$NAME"
    echo "$(date) ABORT: 宿主副本 TOC 校验失败，请检查 $OUT" >&2
    exit 1
fi

prune
if [ -n "$REMOTE_DIR" ]; then
    mkdir -p "$REMOTE_DIR"
    cp "$OUT" "$REMOTE_DIR/$NAME"
    echo "$(date) remote copy -> $REMOTE_DIR/$NAME"
fi
write_state true "weekly pg_dump ok size=${SZ}B" "$NAME"
echo "$(date) pg_dump done: $(du -h "$OUT" | cut -f1) size=${SZ}B total=$(bytes_of)B free=${FREE_GB}G"
