# =====================================================
# 期货预测平台一键启动脚本（含 Docker Desktop 自动拉起）
# 用法：
#   .\start.ps1            启动（引擎不在则自动拉起 Docker Desktop）
#   .\start.ps1 -Build     启动并强制重新构建镜像（代码变更后用）
#   .\start.ps1 -Action stop    停止全部容器
#   .\start.ps1 -Action restart 重启全部容器
#   .\start.ps1 -Action status  查看状态
# =====================================================
param(
    [ValidateSet("start", "stop", "restart", "status")]
    [string]$Action = "start",
    [switch]$Build,
    [int]$Port = 8000
)

$ErrorActionPreference = "Continue"
$Root = $PSScriptRoot
Set-Location $Root

function Write-Step($msg)  { Write-Host "==> $msg" -ForegroundColor Cyan }
function Write-Ok($msg)    { Write-Host "  [OK] $msg" -ForegroundColor Green }
function Write-Warn2($msg) { Write-Host "  [!!] $msg" -ForegroundColor Yellow }

function Test-EngineReady {
    docker info *> $null
    return ($LASTEXITCODE -eq 0)
}

function Wait-Engine([int]$TimeoutSec = 120) {
    if (Test-EngineReady) { return $true }
    Write-Step "Docker 引擎未运行，正在启动 Docker Desktop ..."
    $exe = @(
        "$env:LOCALAPPDATA\Programs\DockerDesktop\Docker Desktop.exe",
        "C:\Program Files\Docker\Docker\Docker Desktop.exe"
    ) | Where-Object { Test-Path $_ } | Select-Object -First 1
    if (-not $exe) {
        Write-Warn2 "未找到 Docker Desktop，请先安装：https://www.docker.com/products/docker-desktop"
        return $false
    }
    Start-Process $exe
    Write-Ok "已启动 Docker Desktop（$exe）"
    Write-Step "等待 Docker 引擎就绪（最多 $TimeoutSec 秒）..."
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        if (Test-EngineReady) { Write-Ok "Docker 引擎已就绪"; return $true }
        Start-Sleep -Seconds 4
        Write-Host "." -NoNewline
    }
    Write-Host ""
    Write-Warn2 "等待超时。若为首次启动 WSL 可能较慢，请稍后重试。"
    return $false
}

function Get-ApiHealth {
    try {
        return Invoke-RestMethod "http://127.0.0.1:$Port/health" -TimeoutSec 4
    } catch { return $null }
}

# ---------- status ----------
if ($Action -eq "status") {
    if (-not (Test-EngineReady)) {
        Write-Warn2 "Docker 引擎未运行"; exit 1
    }
    docker compose ps
    $h = Get-ApiHealth
    if ($h) {
        Write-Ok "API /health: status=$($h.status) ready=$($h.readiness.ready)"
    } else {
        Write-Warn2 "API 未响应（http://127.0.0.1:$Port）"
    }
    exit 0
}

# ---------- stop ----------
if ($Action -eq "stop") {
    if (-not (Test-EngineReady)) { Write-Warn2 "Docker 引擎未运行，无需停止"; exit 0 }
    Write-Step "停止全部容器 ..."
    docker compose down
    Write-Ok "已停止（数据卷保留，重跑 start 即恢复）"
    exit 0
}

# ---------- restart ----------
if ($Action -eq "restart") {
    if (-not (Wait-Engine)) { exit 1 }
    Write-Step "重启全部容器 ..."
    docker compose restart
    exit 0
}

# ---------- start ----------
if (-not (Wait-Engine)) { exit 1 }

Write-Step "启动服务栈（timescaledb + api + scheduler）..."
if ($Build) {
    Write-Host "    （含镜像重建，首次或代码变更后耗时较长）"
    docker compose up -d --build
} else {
    docker compose up -d
}
if ($LASTEXITCODE -ne 0) { Write-Warn2 "docker compose up 失败，请检查上方报错"; exit 1 }

Write-Step "等待容器健康（最多 120 秒）..."
$deadline = (Get-Date).AddSeconds(120)
while ((Get-Date) -lt $deadline) {
    $bad = docker compose ps --format json 2>$null | ForEach-Object { $_ | ConvertFrom-Json } |
        Where-Object { $_.Health -and $_.Health -ne "healthy" }
    if (-not $bad) { break }
    Start-Sleep -Seconds 5
}
docker compose ps --format "table {{.Name}}\t{{.Status}}"

Write-Step "数据就绪门控自检 ..."
$h = $null
$deadline = (Get-Date).AddSeconds(30)
while ((Get-Date) -lt $deadline) {
    $h = Get-ApiHealth
    if ($h) { break }
    Start-Sleep -Seconds 3
}
if ($h) {
    Write-Ok "API 健康: status=$($h.status)"
    if ($h.readiness) {
        Write-Ok "数据就绪: ready=$($h.readiness.ready) 覆盖率=$([math]::Round($h.readiness.ready_ratio * 100))%"
    }
} else {
    Write-Warn2 "API 未响应，查看日志: docker logs qhyc-api --tail 50"
}

Write-Host ""
Write-Host "=========================================" -ForegroundColor DarkGray
Write-Host " 看板:   http://localhost:$Port/" -ForegroundColor White
Write-Host " Swagger: http://localhost:$Port/docs" -ForegroundColor White
Write-Host "=========================================" -ForegroundColor DarkGray
Write-Host ""

# 打开看板
try { Start-Process "http://localhost:$Port/" } catch {}
