#!/bin/sh
# 低峰窗口触发存量 chunk 压缩（由 crontab 调用）。详见 compress_existing_chunks.sql。
#
# ★ 2026-11-10（CL-20261110-14）：日志路径去掉 /var/log 硬编码。
#   宿主 crontab 的作业日志统一落在 /home/ubuntu/qhyc_work/logs（ubuntu 可写），
#   /var/log 默认仅 root 可写——旧版靠 sudo 预创建 + chown 该文件才不报 exit 126，
#   主机重装/迁移即复发，且脚本内重定向把输出全吃掉，外层 qhyc_task_runner.log
#   看不到任何压缩结果。现默认与其余作业同目录，并允许 QHYC_LOG_DIR 覆盖。
#   同时补 ON_ERROR_STOP=1 + 显式失败转发：旧版 psql -f 报错仍退 0，
#   task_runner.py 按 returncode 记 success，压缩没真跑也「全绿」（skill 坑 #9 变体）。
set -eu

LOG_DIR="${QHYC_LOG_DIR:-/home/ubuntu/qhyc_work/logs}"
LOG_FILE="${LOG_DIR}/qhyc_compress.log"
# QHYC_SQL_FILE 仅供等价通道验证（只读探针）覆盖；生产默认走仓库内压缩 SQL。
SQL_FILE="${QHYC_SQL_FILE:-/Docker/qhyc/scripts/db/compress_existing_chunks.sql}"

if ! mkdir -p "$LOG_DIR" 2>/dev/null || [ ! -w "$LOG_DIR" ]; then
    printf '[run_compress] 日志目录不可写: %s\n' "$LOG_DIR" >&2
    exit 1
fi

printf '[%s] start sql=%s log=%s\n' "$(date -u +%FT%TZ)" "$SQL_FILE" "$LOG_FILE" >> "$LOG_FILE"

if docker exec -i qhyc-timescaledb psql -U futures -d futures -v ON_ERROR_STOP=1 -f - \
        < "$SQL_FILE" >> "$LOG_FILE" 2>&1; then
    printf '[%s] OK\n' "$(date -u +%FT%TZ)" >> "$LOG_FILE"
else
    rc=$?
    printf '[%s] FAILED rc=%s\n' "$(date -u +%FT%TZ)" "$rc" >> "$LOG_FILE"
    # 转发到 stderr：外层 crontab 会把 stdout/stderr 收进 qhyc_task_runner.log，
    # 否则失败详情只躺在独立日志里，巡检与流水都看不到。
    printf '[run_compress] FAILED rc=%s log=%s\n' "$rc" "$LOG_FILE" >&2
    tail -n 30 "$LOG_FILE" >&2 || true
    exit "$rc"
fi
