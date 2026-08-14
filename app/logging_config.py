"""日志配置。

日志写入 ~/.tram/tram.log，滚动保留 3 个文件各 2MB。
通过 setup_logging() 在程序启动时调用一次。

另提供 setup_crash_handlers()：安装 faulthandler 与全局/线程级
excepthook，把崩溃堆栈落盘。打包后的 GUI 程序没有控制台，
stderr/stdout 不可用，必须写文件才能在闪退后取回真实原因。
"""

from __future__ import annotations

import faulthandler
import logging
import logging.handlers
import sys
import threading

from .config import CONFIG_DIR

LOG_FILE = CONFIG_DIR / "tram.log"
# faulthandler 专用（它写裸堆栈，不走 logging 格式），避免与滚动日志互相干扰
CRASH_FILE = CONFIG_DIR / "crash.log"

# faulthandler 需要常驻文件句柄，防止被 GC 提前关闭
_crash_fh = None


def setup_logging(level: int = logging.INFO) -> logging.Logger:
    """配置全局日志，返回 root logger。"""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # 文件日志：滚动 3 × 2MB
    file_handler = logging.handlers.RotatingFileHandler(
        LOG_FILE, maxBytes=2_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)

    root = logging.getLogger()
    root.setLevel(level)
    # 避免重复添加 handler
    if not any(
        isinstance(h, logging.handlers.RotatingFileHandler) for h in root.handlers
    ):
        root.addHandler(file_handler)
        # 控制台日志（开发调试用）：打包后的 GUI 程序 stderr 为 None，跳过
        if sys.stderr is not None:
            console_handler = logging.StreamHandler()
            console_handler.setFormatter(fmt)
            root.addHandler(console_handler)

    # 降低第三方库的日志级别
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    return root


def setup_crash_handlers() -> None:
    """安装全局崩溃捕获，把堆栈写入文件。

    覆盖三类情况：
    - faulthandler：致命信号/访问违例时 dump Python 栈到 crash.log；
    - sys.excepthook：主线程未捕获异常写入 tram.log；
    - threading.excepthook：子线程未捕获异常写入 tram.log。

    注意：Qt/PyQt6 把槽函数里的未捕获异常转成 qFatal 消息，
    那条 traceback 由 main.py 的 Qt 消息处理器写入日志，二者互补。
    """
    global _crash_fh
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    try:
        # 需常驻句柄（faulthandler 崩溃时写入），不能用 with 提前关闭
        _crash_fh = open(  # noqa: SIM115
            CRASH_FILE, "a", encoding="utf-8", buffering=1
        )
        faulthandler.enable(file=_crash_fh)
    except Exception:  # pragma: no cover - 启用失败不影响主流程
        logging.getLogger(__name__).debug(
            "faulthandler 启用失败", exc_info=True
        )

    logger = logging.getLogger(__name__)

    def _excepthook(exc_type, exc_value, exc_tb) -> None:
        try:
            logger.critical(
                "主线程未捕获异常", exc_info=(exc_type, exc_value, exc_tb)
            )
            for h in logging.getLogger().handlers:
                h.flush()
        except Exception:
            pass
        # 交还原默认行为（打印并退出）
        if sys.__excepthook__ is not None:
            sys.__excepthook__(exc_type, exc_value, exc_tb)

    def _thread_excepthook(args) -> None:
        # threading.main_thread 的异常走 sys.excepthook，这里只处理子线程
        if args.thread is threading.main_thread():
            return
        try:
            logger.critical(
                "子线程未捕获异常 [%s]",
                args.thread.name if args.thread else "?",
                exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
            )
            for h in logging.getLogger().handlers:
                h.flush()
        except Exception:
            pass

    sys.excepthook = _excepthook
    threading.excepthook = _thread_excepthook
