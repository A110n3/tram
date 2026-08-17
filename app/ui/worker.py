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

from ..core.backend import BackendError, fetch_models, test_connection
from ..core.ocr import ocr_bytes
from ..core.translator import Translator

logger = logging.getLogger(__name__)


class _StopRequested(Exception):
    """用户点击停止时抛出，用于中断流式翻译。"""


class TranslateWorker(QThread):
    token = pyqtSignal(str)
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


class OCRWorker(QThread):
    """在 QThread 中执行 Tesseract 子进程识别。

    QPixmap 非跨线程安全，调用方在主线程先转成 PNG 字节流再传入；
    本线程只做临时文件落盘 + subprocess，无任何 GUI 依赖。
    """

    succeeded = pyqtSignal(str)
    failed = pyqtSignal(str)

    def __init__(self, png: bytes, languages: str, parent=None):
        super().__init__(parent)
        self._png = png
        self._languages = languages

    def run(self) -> None:
        try:
            text = ocr_bytes(self._png, self._languages)
        except Exception as e:  # 二进制缺失/崩溃/超时
            logger.warning("OCR 失败: %s", e, exc_info=True)
            self.failed.emit(str(e))
        else:
            self.succeeded.emit(text)


class TestConnectionWorker(QThread):
    """后台执行 test_connection，避免阻塞 UI。

    设置对话框的「测试连接」按钮与托盘菜单切换目标语言共用。
    """

    ok = pyqtSignal(str)
    err = pyqtSignal(str)

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        use_system_role: bool = True,
        parent=None,
    ):
        super().__init__(parent)
        self._base_url = base_url
        self._api_key = api_key
        self._model = model
        self._use_system_role = use_system_role

    def run(self) -> None:
        try:
            reply = test_connection(
                self._base_url,
                self._api_key,
                self._model,
                use_system_role=self._use_system_role,
            )
            self.ok.emit(reply)
        except BackendError as e:
            self.err.emit(str(e))
        except Exception as e:
            logger.warning("测试连接异常", exc_info=True)
            self.err.emit(str(e))


class ListModelsWorker(QThread):
    """后台执行 list_models（GET /models），避免阻塞 UI。

    设置对话框的「获取模型」按钮使用。
    """

    ok = pyqtSignal(list)
    err = pyqtSignal(str)

    def __init__(self, base_url: str, api_key: str, parent=None):
        super().__init__(parent)
        self._base_url = base_url
        self._api_key = api_key

    def run(self) -> None:
        try:
            models = fetch_models(self._base_url, self._api_key)
            self.ok.emit(models)
        except BackendError as e:
            self.err.emit(str(e))
        except Exception as e:
            logger.warning("获取模型列表异常", exc_info=True)
            self.err.emit(str(e))
