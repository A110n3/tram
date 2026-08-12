@echo off
chcp 65001 >nul
title Tram 离线翻译
cd /d "%~dp0"

echo ============================================
echo   Tram 离线翻译 - 一键启动
echo ============================================
echo.

REM 1. 检查 Python
where python >nul 2>nul
if errorlevel 1 (
    echo [错误] 未找到 Python，请先安装 Python 3.10+ 并勾选 "Add to PATH"。
    echo        下载地址: https://www.python.org/downloads/
    pause
    exit /b 1
)

REM 2. 检查依赖是否已安装（未安装则自动安装）
python -m pip show PyQt6 >nul 2>nul
if errorlevel 1 (
    echo [信息] 首次运行，正在安装依赖...
    python -m pip install -r requirements.txt
    if errorlevel 1 (
        echo [错误] 依赖安装失败，请检查网络后重新运行本脚本。
        pause
        exit /b 1
    )
) else (
    echo [信息] 依赖已就绪。
)

REM 3. 启动应用
echo [信息] 正在启动，请稍候...
echo.
python -m app.main
if errorlevel 1 (
    echo.
    echo [错误] 程序异常退出，请检查上方报错信息。
    pause
)
