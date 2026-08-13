@echo off
REM Tram 一键打包脚本：生成单文件 dist\Tram.exe
REM 前置：pip install ".[dev]"
cd /d %~dp0
python -m PyInstaller tram.spec --noconfirm
if errorlevel 1 (
    echo.
    echo [FAILED] 打包失败
    exit /b 1
)
echo.
echo [OK] 打包完成: dist\Tram.exe
