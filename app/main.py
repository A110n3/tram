"""Tram 离线翻译软件 - 入口。

用法：python -m app.main
"""

from __future__ import annotations

import sys

from PyQt6.QtWidgets import QApplication

from .config import APP_NAME, load_config
from .ui.main_window import MainWindow


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)

    config = load_config()
    # 术语表读取进配置，供翻译编排注入提示词
    from .core import glossary as gs

    config["glossary"] = gs.load_glossary()

    window = MainWindow(config)
    # 主窗口隐藏到托盘；划词翻译由托盘菜单管理
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
