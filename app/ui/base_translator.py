"""热键翻译编排器基类。

抽取划词（SelectionTranslator）与 OCR（OCRTranslator）的公共骨架：
后端生命周期、热键线程生命周期、去重缓存、翻译 worker 管理与取消
免疫。子类只需声明 section / service_name / hotkey_id，并实现
_on_hotkey 触发流程与可选的 worker 扩展点。

必须继承 QObject：热键线程的 triggered 信号通过 Qt 排队连接调度到
主线程，确保 grab_selection / QClipboard 等 GUI 操作在主线程执行。
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Callable
from typing import ClassVar, cast

from PyQt6.QtCore import QObject, pyqtSignal

from ..config import get_default
from ..core.backend import OpenAIBackend
from ..core.hotkey import GlobalHotkeyThread
from .popup import TranslationPopup
from .worker import TranslateWorker
from .worker_util import track_worker

logger = logging.getLogger(__name__)


class BaseHotkeyTranslator(QObject):
    """热键触发的翻译服务基类：热键 -> 取词/识别 -> 翻译 -> 悬浮窗。

    信号：
        hotkey_status(bool, str): 热键状态变化。
            True + 已注册 / False + 错误或引导消息。
    """

    hotkey_status = pyqtSignal(bool, str)

    # 子类必须覆盖：config 配置节名 / 状态消息中的服务名 / RegisterHotKey id
    section: ClassVar[str] = ""
    service_name: ClassVar[str] = ""
    hotkey_id: ClassVar[int] = 1

    def __init__(self, config: dict, parent=None):
        super().__init__(parent)
        self._config = config
        self._backend = self._make_backend()
        self._popup: TranslationPopup | None = None
        self._hotkey_thread: GlobalHotkeyThread | None = None
        self._worker: TranslateWorker | None = None
        self._last_text: str = ""  # 最近一次成功翻译的文本，用于去重
        self._last_result: str = ""  # 对应的译文，重复触发时直接重显
        self._pending_text: str = ""  # 当前正在翻译的文本

    # ---------- 后端 ----------
    def _make_backend(self) -> OpenAIBackend:
        b = self._config.get("backend", {})
        return OpenAIBackend(
            base_url=b.get("base_url", get_default("backend", "base_url")),
            api_key=b.get("api_key", get_default("backend", "api_key")),
            model=b.get("model", get_default("backend", "model")),
            timeout=b.get("timeout", get_default("backend", "timeout")),
        )

    def _close_backend(self) -> None:
        try:
            self._backend.close()
        except Exception:
            logger.debug("关闭 backend 异常", exc_info=True)

    # ---------- 热键生命周期 ----------
    def start(self) -> None:
        """注册全局热键，开启服务。

        先确保旧的监听线程已停止并从所有信号断开，避免信号连接累积
        导致后续线程触发的槽重复执行或状态错乱。旧线程未能退出时
        放弃本次注册并发出状态消息（不静默失败）。
        """
        if not self._config.get(self.section, {}).get(
            "enabled", get_default(self.section, "enabled")
        ):
            return

        self._detach_hotkey_thread()
        # 旧线程未能在 1s 内退出则放弃本次注册，避免线程堆积
        if self._hotkey_thread is not None:
            self.hotkey_status.emit(
                False, f"{self.service_name}热键线程尚未退出，请稍后重试开启"
            )
            return

        hotkey = self._config.get(self.section, {}).get(
            "hotkey", get_default(self.section, "hotkey")
        )
        self._hotkey_thread = GlobalHotkeyThread(hotkey, hotkey_id=self.hotkey_id)
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
                sig.disconnect(cast(Callable, slot))
        # 2. 发送停止信号
        ht.request_quit()
        # 3. 等待线程退出
        if ht.wait(1000):
            self._hotkey_thread = None

    def stop(self) -> None:
        """注销热键，取消进行中的 worker。"""
        self._cancel_workers()
        self._on_stopping()
        self._detach_hotkey_thread()

    def _on_stopping(self) -> None:
        """stop 钩子：子类在停止时清理额外资源（如 OCR 覆盖层）。"""

    def _on_registration_failed(self, msg: str) -> None:
        self.hotkey_status.emit(False, msg)

    def _on_registration_ok(self) -> None:
        hotkey = self._config.get(self.section, {}).get(
            "hotkey", get_default(self.section, "hotkey")
        )
        self.hotkey_status.emit(True, f"{self.service_name}已开启，热键: {hotkey}")

    def _on_hotkey(self) -> None:
        """热键触发流程，子类实现。"""
        raise NotImplementedError

    # ---------- 悬浮窗 ----------
    def _new_popup(self) -> TranslationPopup:
        """创建本轮悬浮窗并接好取消回调。

        auto_hide_ms 目前是划词/OCR 共享的悬浮窗设置，有意取自
        selection 节（OCR 无独立悬浮窗配置，避免设置项重复）。
        """
        sel_cfg = self._config.get("selection", {})
        self._popup = TranslationPopup(
            auto_hide_ms=sel_cfg.get(
                "auto_hide_ms", get_default("selection", "auto_hide_ms")
            )
        )
        # 用户点关闭按钮时真正取消翻译：中断请求、释放被占用的后端
        self._popup.close_requested.connect(self._cancel_workers)
        # 用户点重试按钮时：用同一文本重新请求翻译（完成后/缓存展示亦可）
        self._popup.retry_requested.connect(self._on_retry_requested)
        return self._popup

    # ---------- 翻译 worker ----------
    def _cancel_workers(self) -> bool:
        """取消进行中的 worker，返回是否全部确认退出。

        子类可扩展（先取消自有 worker 再调 super()）。
        返回 False 表示仍有线程未死透，此时不应在同一 backend 上
        发起新请求（共享连接与取消事件会互相干扰）。
        """
        self._pending_text = ""  # 新流程/取消后旧文本不再待翻译
        w = self._worker
        self._worker = None  # 先清引用：即使后续抛异常也不会残留僵尸
        if w is None:
            return True
        try:
            w.request_stop()
            if not w.wait(2000):
                logger.warning("翻译线程 2s 未退出")
                return False
        except RuntimeError:
            return True  # C++ 对象已删除（僵尸包装器），无需清理
        try:
            for sig in (w.token, w.succeeded, w.failed, w.retry):
                sig.disconnect()
        except (TypeError, RuntimeError):
            pass
        return True

    def _try_show_cached(self, stripped: str) -> bool:
        """重复触发同一文本：直接重显缓存译文，返回 True。

        用户可能不小心关掉了浮窗，需要再按热键找回译文；
        直接展示缓存也省去重复调用后端的等待。
        术语表/设置/目标语言变化后缓存已被 invalidate_last_text 清空，
        此处展示的始终是与当前配置匹配的译文。
        """
        if stripped == self._last_text and self._last_result:
            if self._popup:
                self._popup.show_cached(self._last_result)
            return True
        return False

    def _begin_translation(self, text: str) -> None:
        """启动流式翻译 worker（取词/识别成功后调用）。

        注意：不在此处记录 _last_text。翻译失败时用户常会重试
        同一文本，若失败也记录去重，重试会被静默拦截。
        成功/失败分别由 _on_translate_success/_on_translate_failed 更新。
        """
        self._pending_text = text
        if self._popup:
            self._popup.show_loading(can_retry=True)
        self._worker = TranslateWorker(self._backend, self._config, text)
        if self._popup:
            self._worker.token.connect(self._popup.append_token)
        self._worker.succeeded.connect(self._on_translate_success)
        self._worker.failed.connect(self._on_translate_failed)
        self._worker.retry.connect(self._on_translate_retry)
        # 线程结束：先清 Python 引用、再删 C++ 对象。引用必须及时清空，
        # 否则 deleteLater 删掉 C++ 对象后，残留的包装器成为僵尸，
        # 后续任何调用都会 RuntimeError（旧版在槽里直接导致 qFatal 闪退）
        track_worker(self, "_worker", self._worker)
        self._worker.start()

    # ---------- 翻译回调 ----------
    def _on_retry_requested(self) -> None:
        """浮窗重试按钮：用同一文本重新请求翻译。

        翻译相关状态均可点击：进行中（先取消在途请求）、失败后、
        完成后与缓存展示态。完成/缓存态下 _pending_text 可能已被
        清空（如中途点过 ✕），回退从去重缓存取原文。

        重试前先清空去重缓存：重试在途时按热键命中同一文本不应
        重显旧译文；重试成功后缓存由新译文重建，失败则由失败
        路径清空，两种终态下缓存都始终与最后一次结果一致。
        """
        # 先取原文：_cancel_workers 会清空 _pending_text
        text = self._pending_text or self._last_text
        if not text:
            return
        if not self._cancel_workers():
            if self._popup:
                self._popup.show_error(
                    "上一次翻译仍在结束中，请稍后重试", can_retry=False
                )
            return
        self.invalidate_last_text()
        self._begin_translation(text)

    def _on_translate_success(self, result: str) -> None:
        # 成功后才记录去重文本与译文缓存
        self._last_text = self._pending_text
        self._last_result = result
        if self._popup:
            self._popup.set_translation(result)

    def _on_translate_failed(self, message: str) -> None:
        # 失败后清空记录，允许立即重试同一文本
        self._last_text = ""
        self._last_result = ""
        if self._popup:
            self._popup.show_error(message)

    def _on_translate_retry(self) -> None:
        if self._popup:
            # 内部自动重试仍处于翻译阶段，保留手动重试入口
            self._popup.show_loading(can_retry=True)

    # ---------- 去重缓存 ----------
    def invalidate_last_text(self) -> None:
        """清空去重缓存（原文与译文），强制下次重新翻译。

        术语表/设置/目标语言变化后，缓存的译文已过时；用户通常
        立刻用同一段文本验证效果，此时必须重新翻译，不能重显旧译文。
        """
        self._last_text = ""
        self._last_result = ""

    # ---------- 切换模型 ----------
    def rebuild_backend(self) -> None:
        """切换模型后重建后端并重新注册热键。"""
        self.stop()  # stop 内含 _cancel_workers(等待) + _detach_hotkey_thread
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
