"""翻译后台工作线程。

TranslateWorker 在 QThread 中执行 Translator.translate()，
通过信号与 UI 通信（流式 token、分块进度、重试、失败）。
主窗口和划词翻译均复用此 worker。

取消翻译时调用 backend.cancel() 中断底层 HTTP 连接，
无需等待下一个 token 才生效。
"""

from __future__ import annotations

import logging

from PyQt6.QtCore import QThread, pyqtSignal

from ..core.translator import Translator

logger = logging.getLogger(__name__)


class _StopRequested(Exception):
    """用户点击停止时抛出，用于中断流式翻译。"""


class TranslateWorker(QThread):
    token = pyqtSignal(str)
    chunk = pyqtSignal(int, int)
    retry = pyqtSignal()
    succeeded = pyqtSignal(str)
    failed = pyqtSignal(str)

    def __init__(self, translator: Translator, text: str, parent=None):
        super().__init__(parent)
        self._translator = translator
        self._text = text
        self._stop_flag = False

    def request_stop(self) -> None:
        """请求停止：设标志位 + 立即中断底层 HTTP 连接。"""
        self._stop_flag = True
        # 原生取消：立即中断 httpx 流式读取，不等下一个 token
        try:
            self._translator.backend.cancel()
        except Exception:
            logger.debug("backend.cancel 异常", exc_info=True)

    def run(self) -> None:
        def on_token(t: str) -> None:
            if self._stop_flag:
                raise _StopRequested()
            self.token.emit(t)

        try:
            result = self._translator.translate(
                self._text,
                on_token=on_token,
                on_chunk=lambda i, n: self.chunk.emit(i, n),
                on_retry=self.retry.emit,
            )
            if not self._stop_flag:
                self.succeeded.emit(result)
        except _StopRequested:
            pass  # 用户主动停止，静默结束
        except Exception as e:  # 网络错误、后端错误等
            if not self._stop_flag:
                logger.warning("翻译失败: %s", e, exc_info=True)
                self.failed.emit(str(e))
