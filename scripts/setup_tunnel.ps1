# QHYC cloud DB tunnel - persistent + auto-start manager (ASCII only, no CJK)
#   Install/autorun : powershell -ExecutionPolicy Bypass -File scripts/setup_tunnel.ps1
#   Reconnect now   : powershell -ExecutionPolicy Bypass -File scripts/setup_tunnel.ps1 -Reconnect
#   Uninstall       : powershell -ExecutionPolicy Bypass -File scripts/setup_tunnel.ps1 -Uninstall
#   Status          : powershell -ExecutionPolicy Bypass -File scripts/setup_tunnel.ps1 -Status
#
# Why: forwards local 15432 -> cloud timescaledb loopback 5432 so local scripts and
# cloud_local_sync can reach the authoritative cloud DB. ssh -L is session-scoped,
# so it dies on terminal close/reboot; HKCU Run restores it at every logon without
# needing Administrator (Task Scheduler ONLOGON would require elevation).
param([switch]$Uninstall,[switch]$Reconnect,[switch]$Status)
$RunKey="HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
$RunName="QHYC-DB-Tunnel"
$ScriptDir=Split-Path -Parent $MyInvocation.MyCommand.Path
$Runner=Join-Path $ScriptDir "run_db_tunnel.ps1"
$Launch="powershell.exe -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$Runner`""
function KillOld {
  Get-CimInstance Win32_Process -Filter "Name='ssh.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like "*15432:127.0.0.1:5432*" } |
    ForEach-Object { try { Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop } catch {} }
}
function TunUp { Test-NetConnection -ComputerName 127.0.0.1 -Port 15432 -InformationLevel Quiet }
if($Uninstall){
  Remove-ItemProperty -Path $RunKey -Name $RunName -ErrorAction SilentlyContinue
  Unregister-ScheduledTask -TaskName "QHYC-DB-Tunnel" -Confirm:$false -ErrorAction SilentlyContinue
  KillOld
  Write-Host "[OK]   autorun removed and tunnel stopped"
  exit 0
}
if($Status){
  $p=@(Get-CimInstance Win32_Process -Filter "Name='ssh.exe'" -ErrorAction SilentlyContinue |
       Where-Object { $_.CommandLine -like "*15432:127.0.0.1:5432*" })
  $a=(Get-ItemProperty -Path $RunKey -Name $RunName -ErrorAction SilentlyContinue).$RunName
  Write-Host "[INFO] tunnel_procs=$($p.Count) port_up=$(TunUp)"
  Write-Host "[INFO] autorun=$a"
  exit 0
}
if($Reconnect){
  KillOld
  Start-Sleep -Seconds 1
  Start-Process powershell.exe -WindowStyle Hidden -ArgumentList "-NoProfile -ExecutionPolicy Bypass -File `"$Runner`""
  Start-Sleep -Seconds 7
  if(TunUp){ Write-Host "[OK]   tunnel reconnected 15432 -> qhyc:5432" }
  else { Write-Host "[FAIL] reconnect failed"; exit 1 }
  exit 0
}
if(-not (Test-Path $Runner)){ Write-Host "[FAIL] missing $Runner"; exit 1 }
if(-not (Get-Command ssh -ErrorAction SilentlyContinue)){ Write-Host "[FAIL] ssh not found"; exit 1 }
Set-ItemProperty -Path $RunKey -Name $RunName -Value $Launch
Write-Host "[OK]   autorun registered (HKCU Run, runs at logon)"
KillOld
Start-Sleep -Seconds 1
Start-Process powershell.exe -WindowStyle Hidden -ArgumentList "-NoProfile -ExecutionPolicy Bypass -File `"$Runner`""
Start-Sleep -Seconds 7
if(TunUp){ Write-Host "[OK]   tunnel is up (port 15432 reachable)" }
else { Write-Host "[FAIL] tunnel not reachable"; exit 1 }