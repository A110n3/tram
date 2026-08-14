"""Tram 离线翻译软件 - 入口。

用法：python -m app.main
"""

from __future__ import annotations

import logging
import sys

import pywintypes
import win32api
import win32event
import winerror
from PyQt6.QtCore import QMessageLogContext, QtMsgType, qInstallMessageHandler
from PyQt6.QtWidgets import QApplication, QMessageBox

from .config import APP_NAME, load_config
from .logging_config import setup_crash_handlers, setup_logging
from .ui.main_window import MainWindow

logger = logging.getLogger(__name__)
# Qt 内部消息经由该 logger 落入 tram.log
_qt_logger = logging.getLogger("qt")

# 单实例互斥量：防止重复启动互相干扰（抢热键/剪贴板）
_SINGLE_INSTANCE_MUTEX = "Tram_Offline_Translator_SingleInstance"
_mutex_handle = None  # 持引用防 GC 提前释放互斥量


def _acquire_single_instance() -> bool:
    """系统互斥量检查单实例，返回 True 表示本进程是首个实例。

    句柄须在整个进程生命周期内存活，若被 GC 关闭，互斥失效
    第二个实例就能趁虚而入。
    """
    global _mutex_handle
    try:
        _mutex_handle = win32event.CreateMutex(
            None, False, _SINGLE_INSTANCE_MUTEX
        )
    except pywintypes.error:
        logger.warning("单实例互斥量创建失败，放行启动", exc_info=True)
        return True  # 创建失败不阻止启动
    return bool(win32api.GetLastError() != winerror.ERROR_ALREADY_EXISTS)


# QtMsgType -> logging 级别
_QT_LEVELS = {
    QtMsgType.QtDebugMsg: logging.DEBUG,
    QtMsgType.QtInfoMsg: logging.INFO,
    QtMsgType.QtWarningMsg: logging.WARNING,
    QtMsgType.QtCriticalMsg: logging.ERROR,
    QtMsgType.QtFatalMsg: logging.CRITICAL,
}


def _qt_message_handler(
    mode: QtMsgType, context: QMessageLogContext, message: str | None
) -> None:
    """把 Qt 内部消息写入日志文件。

    要点：
    - 不 print 到 stderr：打包后的 GUI 程序 stderr 不可用，信息会丢失；
    - PyQt6 把槽函数中的未捕获异常转成 qFatal，其 traceback 会以
      QtFatalMsg 送达这里，落盘后闪退也能追回真实原因；
    - 静默已知的良性剪贴板 COM 警告（取词流程已自行重试兜底）；
    - 本函数内绝不抛异常：异常逃逸到 Qt C++ 层会直接 abort。
    """
    try:
        msg = str(message or "")
        if "OleSetClipboard" in msg or "OleGetClipboard" in msg:
            return
        _qt_logger.log(_QT_LEVELS.get(mode, logging.WARNING), "%s", msg)
        if mode == QtMsgType.QtFatalMsg:
            # qFatal 随后立即 abort，强制刷盘确保写得上
            for h in logging.getLogger().handlers:
                h.flush()
    except Exception:
        pass


def main() -> int:
    setup_logging()
    setup_crash_handlers()
    logger.info("Tram %s 启动", APP_NAME)
    qInstallMessageHandler(_qt_message_handler)

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)

    if not _acquire_single_instance():
        logger.warning("已有 Tram 实例在运行，本实例退出")
        QMessageBox.information(
            None, APP_NAME, "Tram 已在运行，请在托盘图标处操作。"
        )
        return 0

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
