# 期货全品种（仅小时线）入库巡检：每 30 分钟一次
# 存活则只记录；死亡则清理残留 fetch_fdf 并以 --freq hourly 后台重启
$ErrorActionPreference = 'SilentlyContinue'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8
$ts  = (Get-Date).ToString('yyyy-MM-dd HH:mm:ss')
$log = 'e:\Docker\qhyc\logs\fdf_watch.log'

$null = docker cp 'e:\Docker\qhyc\scripts\fdf_watch.sh' qhyc-api:/tmp/fdf_watch.sh 2>&1
$out = (docker exec qhyc-api sh /tmp/fdf_watch.sh 2>&1) -join ' | '
Add-Content -Path $log -Value "[$ts] $out"

if ($out -notmatch 'ALIVE=1' -and $out -notmatch 'FINISHED=1') {
    Add-Content -Path $log -Value "[$ts] run_fdf 不存活 -> 清理残留并重启(取数+复权)"
    $null = docker cp 'e:\Docker\qhyc\scripts\kill_run_fdf.sh' qhyc-api:/tmp/kill_run_fdf.sh 2>&1
    $null = docker exec qhyc-api sh /tmp/kill_run_fdf.sh 2>&1
    $null = docker exec -d qhyc-api sh /app/runtime/fdf_fetch_adjust.sh
    Start-Sleep -Seconds 5
    $out2 = (docker exec qhyc-api sh /tmp/fdf_watch.sh 2>&1) -join ' | '
    Add-Content -Path $log -Value "[$ts] 重启后: $out2"
}
