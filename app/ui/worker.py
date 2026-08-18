"""翻译后台工作线程。

TranslateWorker 在 QThread 中执行 Translator.translate()，
通过信号与 UI 通信（流式 token、重试、失败）。
划词与 OCR 翻译编排器（base_translator）均复用此 worker。

取消翻译时调用 backend.cancel() 中断底层 HTTP 连接，
无需等待下一个 token 才生效；Translator 由 worker 内部创建并
绑定 should_stop（即本 worker 的停止标志），覆盖退避 sleep 等
cancel() 无法触及的窗口期。
"""

from __future__ import annotations

import logging

from PyQt6.QtCore import QThread, pyqtSignal

from ..core.backend import (
    BackendError,
    OpenAIBackend,
    build_test_messages,
)
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

    def __init__(self, backend: OpenAIBackend, config: dict, text: str, parent=None):
        super().__init__(parent)
        self._backend = backend
        self._config = config
        self._text = text
        self._stop_flag = False

    def request_stop(self) -> None:
        """请求停止：设标志位 + 立即中断底层 HTTP 连接。"""
        self._stop_flag = True
        # 原生取消：立即中断 httpx 流式读取，不等下一个 token
        try:
            self._backend.cancel()
        except Exception:
            logger.debug("backend.cancel 异常", exc_info=True)

    def run(self) -> None:
        translator = Translator(
            self._backend, self._config, should_stop=lambda: self._stop_flag
        )

        def on_token(t: str) -> None:
            if self._stop_flag:
                raise _StopRequested()
            self.token.emit(t)

        try:
            result = translator.translate(
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
    """在 QThread 中执行 RapidOCR 进程内识别。

    QPixmap 非跨线程安全，调用方在主线程先转成 PNG 字节流再传入；
    本线程只做 ONNX 推理，无任何 GUI 依赖。
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
        except Exception as e:  # 引擎未安装/初始化失败/推理异常
            logger.warning("OCR 失败: %s", e, exc_info=True)
            self.failed.emit(str(e))
        else:
            self.succeeded.emit(text)


class TestConnectionWorker(QThread):
    """后台执行连接测试，避免阻塞 UI。

    设置对话框的「测试连接」按钮与托盘菜单切换目标语言共用。
    自持 backend 引用（而非调用 test_connection）以支持取消：
    退出/重开对话框时可立即中断 30s 超时内的阻塞请求。
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
        self._stopped = False
        self._backend: OpenAIBackend | None = None

    def request_stop(self) -> None:
        """请求停止：丢弃结果并中断进行中的请求。"""
        self._stopped = True
        b = self._backend
        if b is not None:
            try:
                b.cancel()
            except Exception:
                logger.debug("测试连接 backend.cancel 异常", exc_info=True)

    def run(self) -> None:
        backend = OpenAIBackend(self._base_url, self._api_key, self._model, timeout=30)
        self._backend = backend
        try:
            reply = backend.chat(
                build_test_messages(self._use_system_role),
                temperature=0.0,
                max_tokens=8,
            )
            if not self._stopped:
                self.ok.emit(reply)
        except Exception as e:
            if not self._stopped:
                if not isinstance(e, BackendError):
                    logger.warning("测试连接异常", exc_info=True)
                self.err.emit(str(e))
        finally:
            self._backend = None
            try:
                backend.close()
            except Exception:
                logger.debug("关闭测试连接 backend 异常", exc_info=True)


class ListModelsWorker(QThread):
    """后台执行 list_models（GET /models），避免阻塞 UI。

    设置对话框的「获取模型」按钮使用。自持 backend 以支持取消。
    """

    ok = pyqtSignal(list)
    err = pyqtSignal(str)

    def __init__(self, base_url: str, api_key: str, parent=None):
        super().__init__(parent)
        self._base_url = base_url
        self._api_key = api_key
        self._stopped = False
        self._backend: OpenAIBackend | None = None

    def request_stop(self) -> None:
        """请求停止：丢弃结果并中断进行中的请求。"""
        self._stopped = True
        b = self._backend
        if b is not None:
            try:
                b.cancel()
            except Exception:
                logger.debug("获取模型 backend.cancel 异常", exc_info=True)

    def run(self) -> None:
        backend = OpenAIBackend(self._base_url, self._api_key, timeout=15)
        self._backend = backend
        try:
            models = backend.list_models()
            if not self._stopped:
                self.ok.emit(models)
        except Exception as e:
            if not self._stopped:
                if not isinstance(e, BackendError):
                    logger.warning("获取模型列表异常", exc_info=True)
                self.err.emit(str(e))
        finally:
            self._backend = None
            try:
                backend.close()
            except Exception:
                logger.debug("关闭获取模型 backend 异常", exc_info=True)
