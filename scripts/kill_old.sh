#!/bin/sh
# 杀掉容器内残留的旧 fetch_fdf 后台进程（排除 run_fdf 编排器与脚本自身）
echo '--- before ---'
for p in /proc/[0-9]*; do
  c=$(cat "$p/cmdline" 2>/dev/null | tr '\0' ' ')
  case "$c" in *fetch_fdf*) echo "  ${p##*/} $c";; esac
done
for p in /proc/[0-9]*; do
  [ "$p" = "/proc/$$" ] && continue
  c=$(cat "$p/cmdline" 2>/dev/null | tr '\0' ' ')
  case "$c" in *fetch_fdf*) kill -9 "${p##*/}" && echo "killed ${p##*/}";; esac
done
echo '--- after ---'
for p in /proc/[0-9]*; do
  c=$(cat "$p/cmdline" 2>/dev/null | tr '\0' ' ')
  case "$c" in *fetch_fdf*) echo "  ${p##*/} STILL ALIVE";; esac
done
echo done
