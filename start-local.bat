@echo off
chcp 65001 >nul
cd /d "%~dp0"

rem Local (non-Docker) launcher for Windows preview of ShopDock.
rem Requires Python 3.11+ on PATH. First run creates .venv and installs deps.

if not exist ".venv\Scripts\python.exe" (
  echo Creating virtual environment...
  python -m venv .venv || goto :fail
  ".venv\Scripts\python.exe" -m pip install --upgrade pip -q || goto :fail
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt || goto :fail
)

if not exist "data" mkdir data
set APP_DATA=%CD%\data
set ALLOWED_HOSTS=localhost,127.0.0.1
set COOKIE_SECURE=0

echo ShopDock starting at http://localhost:8080
start "" "http://localhost:8080"
".venv\Scripts\python.exe" -m uvicorn app.main:app --host 127.0.0.1 --port 8080 --workers 1
goto :eof

:fail
echo Startup failed. Check that Python 3.11+ is installed and on PATH.
pause
exit /b 1
