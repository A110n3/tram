"""划词翻译编排器。

连接热键监听、取词、翻译后端和悬浮窗，管理整个划词翻译生命周期。
与主窗口翻译使用独立 backend 实例，互不干扰。

必须继承 QObject：热键线程的 triggered 信号通过 Qt 排队连接调度到
主线程，确保 grab_selection / QClipboard 等 GUI 操作在主线程执行。
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Callable
from typing import Any, cast

from PyQt6.QtCore import QObject, pyqtSignal

from ..core.backend import OpenAIBackend
from ..core.hotkey import GlobalHotkeyThread
from ..core.selection import grab_selection
from ..core.translator import Translator
from .popup import TranslationPopup
from .worker import TranslateWorker
from .worker_util import track_worker

logger = logging.getLogger(__name__)


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
        self._last_text: str = ""  # 最近一次成功翻译的文本，用于去重
        self._pending_text: str = ""  # 当前正在翻译的文本

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
            logger.debug("关闭 backend 异常", exc_info=True)

    # ---------- 开关 ----------
    def start(self) -> None:
        """注册全局热键，开启划词模式。

        先确保旧的监听线程已停止并从所有信号断开，避免信号连接累积
        导致后续线程触发的槽重复执行或状态错乱。
        """
        sel_cfg = self._config.get("selection", {})
        if not sel_cfg.get("enabled", False):
            return

        self._detach_hotkey_thread()
        # 旧线程未能在 1s 内退出则放弃本次注册，避免线程堆积
        if self._hotkey_thread is not None:
            return

        hotkey = sel_cfg.get("hotkey", "Ctrl+F4")
        self._hotkey_thread = GlobalHotkeyThread(hotkey)
        self._hotkey_thread.triggered.connect(self._on_hotkey)
        self._hotkey_thread.registration_ok.connect(self._on_registration_ok)
        self._hotkey_thread.registration_failed.connect(self._on_registration_failed)
        self._hotkey_thread.start()

    def _detach_hotkey_thread(self) -> None:
        """停止并断开当前热键线程的所有信号连接。

        先发停止信号、再等待线程退出、最后才清理引用。
        """
        if not self._hotkey_thread:
            return
        ht = self._hotkey_thread
        # 1. 断开信号（阻止旧线程的延迟 emission 触发槽）
        for sig, slot in [
            (ht.triggered, self._on_hotkey),
            (ht.registration_ok, self._on_registration_ok),
            (ht.registration_failed, self._on_registration_failed),
        ]:
            with contextlib.suppress(TypeError, RuntimeError):
                sig.disconnect(cast("Callable[..., Any]", slot))
        # 2. 发送停止信号
        ht.request_quit()
        # 3. 等待线程退出
        if ht.wait(1000):
            self._hotkey_thread = None

    def stop(self) -> None:
        """注销热键，取消当前翻译。"""
        self._cancel_worker()
        self._detach_hotkey_thread()

    def _on_registration_failed(self, msg: str) -> None:
        self.hotkey_status.emit(False, msg)

    def _on_registration_ok(self) -> None:
        self.hotkey_status.emit(True, "热键已就绪")

    # ---------- 热键处理 ----------
    def _on_hotkey(self) -> None:
        # 1. 清理旧 popup 和进行中的 worker
        self._cancel_worker()
        if self._popup:
            self._popup.hide()
            self._popup = None

        sel_cfg = self._config.get("selection", {})
        auto_hide = sel_cfg.get("auto_hide_ms", 0)
        self._popup = TranslationPopup(auto_hide_ms=auto_hide)
        # 用户点 ✕ 时真正取消翻译：中断请求、释放被占用的后端
        self._popup.close_requested.connect(self._cancel_worker)
        # 先以极简窗告知"正在捕获"，定位在鼠标旁
        self._popup.show_capturing()

        # 2. 取词（可能阻塞最多 400ms，但用户已看到反馈）
        text = grab_selection()
        if not text or not text.strip():
            if self._popup:
                self._popup.fade_out()
            return

        stripped = text.strip()
        min_chars = sel_cfg.get("min_chars", 2)
        if len(stripped) < min_chars:
            if self._popup:
                self._popup.hide()
            return
        if stripped == self._last_text:
            if self._popup:
                self._popup.hide()
            return
        # 注意：不在此处记录 _last_text。翻译失败时用户常会重试
        # 同一文本，若失败也记录去重，重试会被静默拦截。
        # 成功/失败时分别由 _on_selection_success/_on_selection_failed 更新。
        self._pending_text = stripped

        # 3. 取词成功，切换为"翻译中"状态
        if self._popup:
            self._popup.show_loading()

        translator = Translator(self._backend, self._config)
        self._worker = TranslateWorker(translator, stripped)
        self._worker.token.connect(self._popup.append_token)
        self._worker.succeeded.connect(self._on_selection_success)
        self._worker.failed.connect(self._on_selection_failed)
        self._worker.retry.connect(self._on_retry)
        # 线程结束：先清 Python 引用、再删 C++ 对象。引用必须及时清空，
        # 否则 deleteLater 删掉 C++ 对象后，残留的包装器成为"僵尸"，
        # 后续任何调用都会 RuntimeError（旧版在槽里直接导致 qFatal 闪退）
        track_worker(self, "_worker", self._worker)
        self._worker.start()

    def _cancel_worker(self) -> None:
        """取消进行中的翻译，等待线程退出，断开所有信号。

        对"僵尸"worker（C++ 对象已被 deleteLater 删除）全程免疫。
        """
        w = self._worker
        self._worker = None  # 先清引用：即使后续抛异常也不会残留僵尸
        if w is None:
            return
        try:
            w.request_stop()
            w.wait(2000)  # 等待线程退出，避免资源泄漏
        except RuntimeError:
            return  # C++ 对象已删除，无需清理
        try:
            for sig in (w.token, w.succeeded, w.failed, w.retry):
                sig.disconnect()
        except (TypeError, RuntimeError):
            pass

    # ---------- 翻译回调 ----------
    def _on_selection_success(self, result: str) -> None:
        # 成功后才记录去重文本
        self._last_text = self._pending_text
        if self._popup:
            self._popup.set_translation(result)

    def _on_selection_failed(self, message: str) -> None:
        # 失败后清空记录，允许立即重试同一文本
        self._last_text = ""
        if self._popup:
            self._popup.show_error(message)

    def _on_retry(self) -> None:
        if self._popup:
            self._popup.show_loading()

    # ---------- 切换模型 ----------
    def rebuild_backend(self) -> None:
        """切换模型后重建后端并重新注册热键。"""
        self.stop()  # stop 内含 _cancel_worker(等待) + _detach_hotkey_thread
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
