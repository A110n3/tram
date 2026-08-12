"""Tram 离线翻译软件 - 入口。

用法：python -m app.main
"""

from __future__ import annotations

import logging
import sys

from PyQt6.QtCore import QMessageLogContext, QtMsgType, qInstallMessageHandler
from PyQt6.QtWidgets import QApplication

from .config import APP_NAME, load_config
from .logging_config import setup_logging
from .ui.main_window import MainWindow

logger = logging.getLogger(__name__)


def _qt_message_handler(
    mode: QtMsgType, context: QMessageLogContext, message: str | None
) -> None:
    """过滤 Qt 内部日志，静默已知的良性剪贴板 COM 错误。

    剪贴板在取词流程中已通过重试+静默兜底处理，Qt 自行打印的
    OleSetClipboard / OleGetClipboard 警告对用户无意义，直接丢弃。
    """
    msg = str(message or "")
    if "OleSetClipboard" in msg or "OleGetClipboard" in msg:
        return
    # 其余消息写回 stderr，保持默认行为
    print(msg, file=sys.stderr)


def main() -> int:
    setup_logging()
    logger.info("Tram %s 启动", APP_NAME)
    qInstallMessageHandler(_qt_message_handler)

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)

    config = load_config()
    # 术语表读取进配置，供翻译编排注入提示词
    from .core import glossary as gs

    try:
        config["glossary"] = gs.load_glossary()
    except Exception:
        logger.warning("术语表加载失败，回退为空", exc_info=True)
        config["glossary"] = []

    window = MainWindow(config)  # noqa: F841 - 保持引用防止 GC
    # 主窗口隐藏到托盘；划词翻译由托盘菜单管理
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
