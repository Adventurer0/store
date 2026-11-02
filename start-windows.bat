@echo off
chcp 65001 >nul
cd /d "%~dp0"
docker compose up -d --build
if errorlevel 1 (
  echo 启动失败，请确认 Docker Desktop 已启动，并查看上方错误。
  pause
  exit /b 1
)
echo ShopDock 正在启动。请打开 http://localhost:8080
start "" "http://localhost:8080"
pause
