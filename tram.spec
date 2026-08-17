# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：生成单文件 dist/Tram.exe。

构建：python -m PyInstaller tram.spec   （或直接运行 build.bat）
"""

import importlib.util

from PyInstaller.utils.hooks import collect_all, collect_submodules

# pywin32 部分子模块为动态导入，显式收集避免运行期缺失
hiddenimports = collect_submodules("win32api") + [
    "win32timezone",
]

# OCR 引擎（rapidocr 内置 .onnx 模型数据 + onnxruntime DLL/pyd）。
# collect_all 一次收齐 datas/binaries/hiddenimports；未安装时降级，
# 打包产物仅少 OCR 功能、不影响划词翻译。
ocr_datas: list = []
ocr_binaries: list = []
ocr_hiddenimports: list = []
if importlib.util.find_spec("rapidocr") and importlib.util.find_spec("onnxruntime"):
    for _pkg in ("rapidocr", "onnxruntime"):
        _d, _b, _h = collect_all(_pkg)
        ocr_datas += _d
        ocr_binaries += _b
        ocr_hiddenimports += _h
else:
    print("WARNING: rapidocr/onnxruntime 未安装，打包产物将不含 OCR 引擎")

a = Analysis(
    ["launcher.py"],
    pathex=["."],
    binaries=ocr_binaries,
    datas=ocr_datas,
    hiddenimports=hiddenimports + ocr_hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="Tram",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,  # GUI 程序，无控制台窗口
    icon="assets/tram.ico",
)
