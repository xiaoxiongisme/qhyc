#!/bin/sh
# 杀掉 run_fdf 编排器及其 fetch_fdf 子进程（排除脚本自身）
echo '--- before ---'
for p in /proc/[0-9]*; do
  c=$(cat "$p/cmdline" 2>/dev/null | tr '\0' ' ')
  case "$c" in
    *run_fdf*|*fetch_fdf*) echo "  ${p##*/}: $c";;
  esac
done
for p in /proc/[0-9]*; do
  [ "$p" = "/proc/$$" ] && continue
  c=$(cat "$p/cmdline" 2>/dev/null | tr '\0' ' ')
  case "$c" in
    *run_fdf*|*fetch_fdf*) kill -9 "${p##*/}" 2>/dev/null && echo "killed ${p##*/}";;
  esac
done
sleep 2
echo '--- after ---'
for p in /proc/[0-9]*; do
  c=$(cat "$p/cmdline" 2>/dev/null | tr '\0' ' ')
  case "$c" in
    *run_fdf*|*fetch_fdf*) echo "  ${p##*/} STILL ALIVE";;
  esac
done
echo done
