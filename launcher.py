"""PyInstaller 打包入口。

app/main.py 使用包内相对导入，无法直接作为 PyInstaller 入口脚本，
此文件作为顶层入口转发到 app.main:main。
"""

from __future__ import annotations

import sys

from app.main import main

if __name__ == "__main__":
    sys.exit(main())
