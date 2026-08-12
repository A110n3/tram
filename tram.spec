# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：生成单文件 dist/Tram.exe。

构建：python -m PyInstaller tram.spec   （或直接运行 build.bat）
"""

from PyInstaller.utils.hooks import collect_submodules

# pywin32 部分子模块为动态导入，显式收集避免运行期缺失
hiddenimports = collect_submodules("win32api") + [
    "win32timezone",
]

a = Analysis(
    ["launcher.py"],
    pathex=["."],
    binaries=[],
    datas=[],
    hiddenimports=hiddenimports,
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
