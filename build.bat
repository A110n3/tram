@echo off
REM Tram 一键打包脚本：生成单文件 dist\Tram.exe
REM 前置：pip install ".[dev,ocr]"
REM 优先使用项目 .venv（OCR 依赖装在 venv 里，系统 python 缺失时
REM 产物会不含 OCR 引擎且体积偏小）
cd /d %~dp0
set "PYTHON=python"
if exist ".venv\Scripts\python.exe" set "PYTHON=.venv\Scripts\python.exe"
"%PYTHON%" -m PyInstaller tram.spec --noconfirm
if errorlevel 1 (
    echo.
    echo [FAILED] 打包失败
    exit /b 1
)
echo.
echo [OK] 打包完成: dist\Tram.exe
