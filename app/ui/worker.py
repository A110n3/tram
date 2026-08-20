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
from typing import Any

from PyQt6.QtCore import QThread, pyqtSignal

from ..core.backend import (
    BackendError,
    OpenAIBackend,
    build_test_messages,
)
from ..core.ocr import OCRError, ocr_bytes
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


class MonitorWorker(QThread):
    """在 QThread 中执行区域监控循环：PIL 截图 + 漏斗状态机。

    循环完全脱离 GUI 线程（PIL ImageGrab 可在后台线程调用），
    周期 = max(interval, 单帧处理耗时)：OCR 慢于间隔时自动降频，
    不排队积压。new_text 信号携带漏斗产出的新文本（未归一化），
    由编排器做丢旧保新翻译。
    """

    new_text = pyqtSignal(str)
    failed = pyqtSignal(str)

    def __init__(
        self,
        bbox: tuple[int, int, int, int],
        params: Any,
        parent=None,
    ):
        super().__init__(parent)
        self._bbox = bbox  # 主屏物理像素 (left, top, right, bottom)
        self._params = params  # MonitorParams
        self._stop_flag = False

    def request_stop(self) -> None:
        self._stop_flag = True

    def run(self) -> None:
        import time

        import numpy as np
        from PIL import ImageGrab

        from ..core.monitor import RegionMonitorState
        from ..core.ocr import ocr_lines

        state = RegionMonitorState(params=self._params, ocr=ocr_lines)
        interval = max(int(self._params.interval_ms), 100) / 1000.0
        try:
            while not self._stop_flag:
                start = time.monotonic()
                # ImageGrab 返回 RGB；转 BGR 以对齐 cv2/RapidOCR 约定
                pil_img = ImageGrab.grab(bbox=self._bbox)
                frame = np.asarray(pil_img)[:, :, ::-1]
                text = state.process(frame)
                if text and not self._stop_flag:
                    self.new_text.emit(text)
                # 睡满剩余周期；OCR 慢于间隔时不睡（自然降频）
                remaining = interval - (time.monotonic() - start)
                if remaining > 0:
                    time.sleep(remaining)
        except OCRError as e:
            if not self._stop_flag:
                logger.warning("监控 OCR 失败: %s", e, exc_info=True)
                self.failed.emit(str(e))
        except Exception as e:  # 截图失败（锁屏/权限）等
            if not self._stop_flag:
                logger.warning("监控循环异常: %s", e, exc_info=True)
                self.failed.emit(f"监控异常: {e}")


class _BackendRequestWorker(QThread):
    """后台执行 backend 请求的通用基类，避免阻塞 UI。

    子类只需指定 backend 参数和请求操作，统一处理取消、清理逻辑。
    """

    ok = pyqtSignal(object)  # 结果类型由子类决定
    err = pyqtSignal(str)

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str = "",
        timeout: int = 30,
        parent=None,
    ):
        super().__init__(parent)
        self._base_url = base_url
        self._api_key = api_key
        self._model = model
        self._timeout = timeout
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
                logger.debug("%s backend.cancel 异常", self._log_name(), exc_info=True)

    def _log_name(self) -> str:
        """子类覆盖以提供具体操作名称，用于日志。"""
        return "后台请求"

    def _execute_request(self, backend: OpenAIBackend) -> object:
        """子类覆盖以执行具体请求操作，返回结果。"""
        raise NotImplementedError

    def run(self) -> None:
        backend = OpenAIBackend(
            self._base_url, self._api_key, self._model, timeout=self._timeout
        )
        self._backend = backend
        try:
            result = self._execute_request(backend)
            if not self._stopped:
                self.ok.emit(result)
        except Exception as e:
            if not self._stopped:
                if not isinstance(e, BackendError):
                    logger.warning("%s异常", self._log_name(), exc_info=True)
                self.err.emit(str(e))
        finally:
            self._backend = None
            try:
                backend.close()
            except Exception:
                logger.debug("关闭%s backend 异常", self._log_name(), exc_info=True)


class TestConnectionWorker(_BackendRequestWorker):
    """后台执行连接测试，避免阻塞 UI。

    设置对话框的「测试连接」按钮与托盘菜单切换目标语言共用。
    自持 backend 引用（而非调用 test_connection）以支持取消：
    退出/重开对话框时可立即中断 30s 超时内的阻塞请求。

    timeout 可覆盖（默认 30s）：启动预热复用本 worker 等待本地
    后端加载模型（Lemonade/Ollama 冷启动可达数分钟，30s 必超时）。
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        use_system_role: bool = True,
        timeout: int = 30,
        parent=None,
    ):
        super().__init__(base_url, api_key, model, timeout, parent)
        self._use_system_role = use_system_role

    def _log_name(self) -> str:
        return "测试连接"

    def _execute_request(self, backend: OpenAIBackend) -> str:
        return backend.chat(
            build_test_messages(self._use_system_role),
            temperature=0.0,
            max_tokens=8,
        )


class ListModelsWorker(_BackendRequestWorker):
    """后台执行 list_models（GET /models），避免阻塞 UI。

    设置对话框的「获取模型」按钮使用。自持 backend 以支持取消。
    """

    def __init__(self, base_url: str, api_key: str, parent=None):
        super().__init__(base_url, api_key, timeout=15, parent=parent)

    def _log_name(self) -> str:
        return "获取模型列表"

    def _execute_request(self, backend: OpenAIBackend) -> list[str]:
        return backend.list_models()
