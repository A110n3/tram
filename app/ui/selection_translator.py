"""划词翻译编排器。

连接热键监听、取词、翻译后端和悬浮窗，管理整个划词翻译生命周期。
与主窗口翻译使用独立 backend 实例，互不干扰。

必须继承 QObject：热键线程的 triggered 信号通过 Qt 排队连接调度到
主线程，确保 grab_selection / QClipboard 等 GUI 操作在主线程执行。
"""

from __future__ import annotations

from PyQt6.QtCore import QObject, pyqtSignal

from ..config import save_config
from ..core.backend import OpenAIBackend
from ..core.hotkey import GlobalHotkeyThread
from ..core.selection import grab_selection
from ..core.translator import Translator
from .popup import TranslationPopup
from .worker import TranslateWorker


class SelectionTranslator(QObject):
    """管理划词翻译全流程：热键 -> 取词 -> 翻译 -> 悬浮窗。

    信号：
        hotkey_status(bool, str): 热键状态变化。
            True + "已注册" / False + 错误消息。
    """

    hotkey_status = pyqtSignal(bool, str)

    def __init__(self, config: dict, parent=None):
        super().__init__(parent)
        self._config = config
        self._backend = self._make_backend()
        self._popup: TranslationPopup | None = None
        self._hotkey_thread: GlobalHotkeyThread | None = None
        self._worker: TranslateWorker | None = None
        self._last_text: str = ""

    # ---------- 后端 ----------
    def _make_backend(self) -> OpenAIBackend:
        b = self._config.get("backend", {})
        return OpenAIBackend(
            base_url=b.get("base_url", ""),
            api_key=b.get("api_key", "ollama"),
            model=b.get("model", ""),
            timeout=int(b.get("timeout", 180)),
        )

    def _close_backend(self) -> None:
        try:
            self._backend.close()
        except Exception:
            pass

    # ---------- 开关 ----------
    def start(self) -> None:
        """注册全局热键，开启划词模式。"""
        sel_cfg = self._config.get("selection", {})
        if not sel_cfg.get("enabled", False):
            return

        self.stop()

        hotkey = sel_cfg.get("hotkey", "Ctrl+Shift+T")
        self._hotkey_thread = GlobalHotkeyThread(hotkey)
        self._hotkey_thread.triggered.connect(self._on_hotkey)
        self._hotkey_thread.registration_ok.connect(self._on_registration_ok)
        self._hotkey_thread.registration_failed.connect(self._on_registration_failed)
        self._hotkey_thread.start()

    def stop(self) -> None:
        """注销热键，取消当前翻译。"""
        self._cancel_worker()
        if self._hotkey_thread:
            ht = self._hotkey_thread
            self._hotkey_thread = None
            ht.request_quit()
            ht.wait(1000)

    def _on_registration_failed(self, msg: str) -> None:
        self.hotkey_status.emit(False, msg)

    def _on_registration_ok(self) -> None:
        self.hotkey_status.emit(True, f"热键已就绪")

    # ---------- 热键处理 ----------
    def _on_hotkey(self) -> None:
        text = grab_selection()
        if not text or not text.strip():
            return

        stripped = text.strip()
        sel_cfg = self._config.get("selection", {})
        min_chars = sel_cfg.get("min_chars", 2)
        if len(stripped) < min_chars:
            return
        if stripped == self._last_text:
            return
        self._last_text = stripped

        self._cancel_worker()

        auto_hide = sel_cfg.get("auto_hide_ms", 0)
        self._popup = TranslationPopup(auto_hide_ms=auto_hide)
        self._popup.show_loading(stripped)

        translator = Translator(self._backend, self._config)
        self._worker = TranslateWorker(translator, stripped)
        self._worker.token.connect(self._popup.append_token)
        self._worker.succeeded.connect(self._on_selection_success)
        self._worker.failed.connect(self._on_selection_failed)
        self._worker.retry.connect(self._on_retry)
        self._worker.start()

    def _cancel_worker(self) -> None:
        """取消进行中的翻译并断开所有信号。"""
        if self._worker:
            self._worker.request_stop()
            try:
                self._worker.token.disconnect()
                self._worker.succeeded.disconnect()
                self._worker.failed.disconnect()
                self._worker.retry.disconnect()
            except TypeError:
                pass
        self._worker = None

    # ---------- 翻译回调 ----------
    def _on_selection_success(self, result: str) -> None:
        if self._popup:
            self._popup.set_translation(result)

    def _on_selection_failed(self, message: str) -> None:
        if self._popup:
            self._popup.show_error(message)

    def _on_retry(self) -> None:
        if self._popup:
            self._popup.show_loading()

    # ---------- 切换模型 ----------
    def rebuild_backend(self) -> None:
        """切换模型后重建后端并重新注册热键。"""
        self.stop()
        self._close_backend()
        self._backend = self._make_backend()
        self.start()

    # ---------- 清理 ----------
    def shutdown(self) -> None:
        self.stop()
        if self._popup:
            self._popup.hide()
            self._popup = None
        self._close_backend()
