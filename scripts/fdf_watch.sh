#!/bin/sh
# 容器内执行：检查 run_fdf 是否存活、上一轮是否已正常跑完，并输出现状。
# 仅输出，不做重启（重启由宿主 PowerShell 完成）。
alive=0
for p in /proc/[0-9]*; do
  [ "$p" = "/proc/$$" ] && continue
  c=$(cat "$p/cmdline" 2>/dev/null | tr '\0' ' ')
  case "$c" in *run_fdf*) alive=1;; esac
done
echo "ALIVE=$alive"

# 上一轮是否已正常结束（"全流程结束" 出现在最后一次 "开始取数" 之后）
ls_line=$(grep -n '开始取数' /app/runtime/fdf_all.log 2>/dev/null | tail -n 1 | cut -d: -f1)
le_line=$(grep -n '全流程结束' /app/runtime/fdf_all.log 2>/dev/null | tail -n 1 | cut -d: -f1)
fin=0
if [ -n "$le_line" ] && { [ -z "$ls_line" ] || [ "$le_line" -gt "$ls_line" ]; }; then
  fin=1
fi
echo "FINISHED=$fin"

n_skip=$(grep -c '续跑跳过' /app/runtime/fdf_all.log 2>/dev/null)
echo "SKIP=$n_skip"
echo "LAST=$(tail -n 1 /app/runtime/fdf_all.log 2>/dev/null)"
