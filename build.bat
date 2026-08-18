@echo off
REM Tram build script: produces single-file dist\Tram.exe
REM Prerequisite: pip install ".[dev,ocr]"
REM Prefers the project .venv (OCR deps live in the venv; a system
REM python without them yields an exe missing the OCR engine).
REM NOTE: keep this file pure ASCII - Chinese comments break cmd.exe
REM batch parsing under GBK/UTF-8 codepages (garbled bytes make comment
REM text get executed as commands).
cd /d %~dp0
set "PYTHON=python"
if exist ".venv\Scripts\python.exe" set "PYTHON=.venv\Scripts\python.exe"
"%PYTHON%" -m PyInstaller tram.spec --noconfirm
if errorlevel 1 (
    echo.
    echo [FAILED] build failed
    exit /b 1
)
echo.
echo [OK] build done: dist\Tram.exe
