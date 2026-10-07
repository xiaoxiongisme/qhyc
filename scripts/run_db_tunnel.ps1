$ErrorActionPreference = "Continue"
$LocalPort=15432; $RemoteHost="127.0.0.1"; $RemotePort=5432; $SshHost="qhyc"
Get-CimInstance Win32_Process -Filter "Name='ssh.exe'" -ErrorAction SilentlyContinue |
  Where-Object { $_.CommandLine -like "*${LocalPort}:${RemoteHost}:${RemotePort}*" } |
  ForEach-Object { try { Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop } catch {} }
Start-Sleep -Milliseconds 800
& ssh -N -L "${LocalPort}:${RemoteHost}:${RemotePort}" -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -o StrictHostKeyChecking=accept-new $SshHost
exit $LASTEXITCODE