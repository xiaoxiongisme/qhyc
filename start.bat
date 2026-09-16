@echo off
REM 期货预测平台一键启动（双击或命令行均可）
REM 实际逻辑在 start.ps1：自动拉起 Docker Desktop -> compose up -> 健康检查 -> 打开看板
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" %*
