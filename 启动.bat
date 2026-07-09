@echo off
setlocal enabledelayedexpansion

:: 从 main.py 读取版本号
for /f "usebackq tokens=3 delims= " %%v in (`findstr /b "VERSION" main.py`) do set APP_VER=%%v
set APP_VER=!APP_VER:"=!

if "!APP_VER!"=="" set APP_VER=?

title QCE2ChatLab Setup

echo ============================================
echo   QCE2ChatLab - QQ Chat Sync Tool v!APP_VER!
echo ============================================
echo.

:: Check Python
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found. Please install Python 3.9+
    echo          Download: https://www.python.org/downloads/
    pause
    exit /b 1
)

echo [OK] Python found
echo.

:: Try pip3 first (some systems), fallback to pip
set PIP_CMD=pip
pip3 --version >nul 2>&1
if not errorlevel 1 set PIP_CMD=pip3

echo [*] Installing dependencies...
%PIP_CMD% install httpx fastapi uvicorn pydantic -q

if errorlevel 1 (
    echo [ERROR] Failed to install dependencies.
    echo         Please run this command manually:
    echo         pip install httpx fastapi uvicorn pydantic
    pause
    exit /b 1
)

echo [OK] Dependencies installed
echo.
echo [*] Starting QCE2ChatLab...
echo     Browser will open at http://127.0.0.1:15520
echo     Close this window to stop the service.
echo.

start http://127.0.0.1:15520

python main.py

pause
