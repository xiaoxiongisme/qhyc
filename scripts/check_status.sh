#!/bin/sh
echo '=== run_fdf alive? ==='
for p in /proc/[0-9]*; do
  c=$(cat "$p/cmdline")
  case "$c" in *run_fdf*) echo "ALIVE ${p##*/}";; esac
done
echo '=== old fetch_fdf? ==='
for p in /proc/[0-9]*; do
  c=$(cat "$p/cmdline")
  case "$c" in *fetch_fdf*) echo "OLD ${p##*/}";; esac
done
echo '=== 已出现品种数(去重, 1h+15min 同算1) ==='
grep -oE '[A-Z]+\.[a-zA-Z]+[0-9]' /app/runtime/fdf_all.log | sed -E 's/[0-9]+$//' | sort -u | wc -l
echo '=== 最新抓取合约 ==='
grep -oE '[A-Z]+\.[a-zA-Z]+[0-9]+' /app/runtime/fdf_all.log | tail -1
echo '=== 完成/异常标记统计 ==='
echo -n "[完成] 次数: "; grep -c '完成' /app/runtime/fdf_all.log
echo -n "[异常] 次数: "; grep -c '异常' /app/runtime/fdf_all.log
echo -n "[续跑跳过] 次数: "; grep -c '续跑跳过' /app/runtime/fdf_all.log
echo '=== 日志尾部 ==='
tail -n 8 /app/runtime/fdf_all.log
