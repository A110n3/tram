"""日志配置。

日志写入 ~/.tram/tram.log，滚动保留 3 个文件各 2MB。
通过 setup_logging() 在程序启动时调用一次。
"""

from __future__ import annotations

import logging
import logging.handlers

from .config import CONFIG_DIR

LOG_FILE = CONFIG_DIR / "tram.log"


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

    # 控制台日志（开发调试用）
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(fmt)

    root = logging.getLogger()
    root.setLevel(level)
    # 避免重复添加 handler
    if not any(
        isinstance(h, logging.handlers.RotatingFileHandler) for h in root.handlers
    ):
        root.addHandler(file_handler)
        root.addHandler(console_handler)

    # 降低第三方库的日志级别
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    return root
