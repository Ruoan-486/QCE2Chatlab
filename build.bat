@echo off
chcp 65001 >nul
echo ============================================
echo   QCE2ChatLab - 打包为 Windows EXE
echo ============================================
echo.

REM 检查 Python
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo ❌ 未检测到 Python
    pause
    exit /b 1
)

echo 📦 安装打包工具...
pip install pyinstaller

echo 🔨 开始打包...
pyinstaller --onefile --windowed --add-data "frontend;frontend" --name QCE2ChatLab main.py

if %errorlevel% equ 0 (
    echo.
    echo ============================================
    echo   ✅ 打包完成!
    echo   文件在: dist\QCE2ChatLab.exe
    echo   复制到任意位置双击即可运行
    echo ============================================
) else (
    echo ❌ 打包失败
)

pause
