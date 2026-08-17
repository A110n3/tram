"""OCR 识图翻译编排器。

结构与 SelectionTranslator 一一对应：独立后端实例、独立热键线程
（hotkey_id=2，与划词热键 id=1 区分）、独立去重缓存。

流程：热键 -> 截主屏 -> 全屏覆盖层框选 -> 裁剪 -> OCRWorker 识别
-> 复用 TranslateWorker 流式翻译 -> TranslationPopup 展示。
翻译管线（loading/流式/错误/缓存重显）与划词翻译完全共用。
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Callable
from typing import Any, cast

from PyQt6.QtCore import QObject, QRect, pyqtSignal
from PyQt6.QtGui import QGuiApplication, QPixmap

from ..core.backend import OpenAIBackend
from ..core.hotkey import GlobalHotkeyThread
from ..core.ocr import is_rapidocr_available, pixmap_to_png
from ..core.translator import Translator
from .popup import TranslationPopup
from .region_overlay import RegionOverlay
from .worker import OCRWorker, TranslateWorker
from .worker_util import track_worker

logger = logging.getLogger(__name__)

# OCR 热键的 RegisterHotKey id，与划词热键（id=1）区分
OCR_HOTKEY_ID = 2


class OCRTranslator(QObject):
    """管理 OCR 识图翻译全流程：热键 -> 框选 -> 识别 -> 翻译 -> 悬浮窗。

    信号：
        hotkey_status(bool, str): 热键/环境状态变化。
            True + "已注册" / False + 错误或引导消息。
    """

    hotkey_status = pyqtSignal(bool, str)

    def __init__(self, config: dict, parent=None):
        super().__init__(parent)
        self._config = config
        self._backend = self._make_backend()
        self._popup: TranslationPopup | None = None
        self._hotkey_thread: GlobalHotkeyThread | None = None
        self._overlay: RegionOverlay | None = None
        # 热键时刻截取的冻结截图：覆盖层背景与裁剪必须使用同一张
        self._screenshot: QPixmap | None = None
        self._ocr_worker: OCRWorker | None = None
        self._worker: TranslateWorker | None = None
        self._last_text: str = ""  # 最近一次成功翻译的 OCR 文本，用于去重
        self._last_result: str = ""
        self._pending_text: str = ""

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
        """注册全局热键，开启 OCR 模式。"""
        ocr_cfg = self._config.get("ocr", {})
        if not ocr_cfg.get("enabled", False):
            return

        self._detach_hotkey_thread()
        if self._hotkey_thread is not None:
            return  # 旧线程未退出，放弃本次注册，避免线程堆积

        hotkey = ocr_cfg.get("hotkey", "Ctrl+Shift+F4")
        self._hotkey_thread = GlobalHotkeyThread(hotkey, hotkey_id=OCR_HOTKEY_ID)
        self._hotkey_thread.triggered.connect(self._on_hotkey)
        self._hotkey_thread.registration_ok.connect(self._on_registration_ok)
        self._hotkey_thread.registration_failed.connect(self._on_registration_failed)
        self._hotkey_thread.start()

    def _detach_hotkey_thread(self) -> None:
        """停止并断开当前热键线程的所有信号连接。"""
        if not self._hotkey_thread:
            return
        ht = self._hotkey_thread
        for sig, slot in [
            (ht.triggered, self._on_hotkey),
            (ht.registration_ok, self._on_registration_ok),
            (ht.registration_failed, self._on_registration_failed),
        ]:
            with contextlib.suppress(TypeError, RuntimeError):
                sig.disconnect(cast("Callable[..., Any]", slot))
        ht.request_quit()
        if ht.wait(1000):
            self._hotkey_thread = None

    def stop(self) -> None:
        """注销热键，取消当前识别/翻译，关闭覆盖层。"""
        self._cancel_workers()
        self._close_overlay()
        self._detach_hotkey_thread()

    def _on_registration_failed(self, msg: str) -> None:
        self.hotkey_status.emit(False, msg)

    def _on_registration_ok(self) -> None:
        hotkey = self._config.get("ocr", {}).get("hotkey", "Ctrl+Shift+F4")
        self.hotkey_status.emit(True, f"OCR 已开启，热键: {hotkey}")

    # ---------- 热键处理 ----------
    def _on_hotkey(self) -> None:
        # 1. 清理旧 popup、进行中的 worker 与残留覆盖层
        self._cancel_workers()
        if self._popup:
            self._popup.hide()
            self._popup = None
        self._close_overlay()

        # 2. OCR 引擎缺失时托盘引导，不弹覆盖层
        if not is_rapidocr_available():
            self.hotkey_status.emit(
                False,
                'OCR 引擎未安装，请运行 pip install "tram[ocr]"',
            )
            return

        # 3. 截主屏（必须在覆盖层显示之前，避免把遮罩截进去）
        screen = QGuiApplication.primaryScreen()
        if screen is None:
            return
        # grabWindow(0) 抓取整个屏幕/桌面窗口；stub 声明为 voidptr，
        # 运行时传 int 0 即可（PyQt6 标准用法）
        self._screenshot = screen.grabWindow(0)  # type: ignore[arg-type]

        # 4. 弹全屏选区覆盖层，结果由 _on_region/_close_overlay_slot 接收
        overlay = RegionOverlay(self._screenshot)
        self._overlay = overlay
        overlay.region_selected.connect(self._on_region)
        overlay.cancelled.connect(self._close_overlay_slot)
        overlay.showFullScreen()
        overlay.raise_()
        overlay.activateWindow()

    def _close_overlay(self) -> None:
        """主动关闭覆盖层（新热键打断 / stop 路径）。"""
        ov = self._overlay
        self._overlay = None
        self._screenshot = None
        if ov is None:
            return
        with contextlib.suppress(RuntimeError, TypeError):
            ov.cancelled.disconnect()
        with contextlib.suppress(RuntimeError, TypeError):
            ov.region_selected.disconnect()
        with contextlib.suppress(RuntimeError):
            ov.close()
            ov.deleteLater()

    def _close_overlay_slot(self) -> None:
        """覆盖层 cancelled 信号：仅清引用，关闭由覆盖层自行完成。"""
        self._overlay = None
        self._screenshot = None

    # ---------- 识别 ----------
    def _on_region(self, region: QRect) -> None:
        self._overlay = None  # 覆盖层已自行 close + deleteLater
        screenshot = self._screenshot
        self._screenshot = None
        if screenshot is None or screenshot.isNull():
            return

        # 选区为覆盖层本地逻辑坐标，截图 pixmap 是设备像素，
        # 裁剪时按 devicePixelRatio 换算（HiDPI 兼容）
        dpr = screenshot.devicePixelRatio()
        src = QRect(
            round(region.x() * dpr),
            round(region.y() * dpr),
            round(region.width() * dpr),
            round(region.height() * dpr),
        )
        src = src.intersected(QRect(0, 0, screenshot.width(), screenshot.height()))
        if src.isEmpty():
            return

        # QPixmap 非跨线程安全：在主线程完成裁剪 + PNG 编码
        try:
            png = pixmap_to_png(screenshot.copy(src))
        except Exception as e:
            logger.warning("OCR 预处理失败: %s", e, exc_info=True)
            return

        # 弹窗在框选结束（鼠标释放）后出现，跟随当前光标定位
        sel_cfg = self._config.get("selection", {})
        self._popup = TranslationPopup(auto_hide_ms=sel_cfg.get("auto_hide_ms", 0))
        self._popup.close_requested.connect(self._cancel_workers)
        self._popup.show_loading("识别中…")

        languages = self._config.get("ocr", {}).get("languages", "ch")
        w = OCRWorker(png, languages)
        self._ocr_worker = w
        w.succeeded.connect(self._on_ocr_ok)
        w.failed.connect(self._on_ocr_failed)
        track_worker(self, "_ocr_worker", w)
        w.start()

    def _on_ocr_ok(self, text: str) -> None:
        self._ocr_worker = None
        if not self._popup:
            return
        stripped = text.strip()
        min_chars = int(self._config.get("ocr", {}).get("min_chars", 2))
        if len(stripped) < max(min_chars, 1):
            self._popup.fade_out("未识别到文字")
            return
        if stripped == self._last_text and self._last_result:
            # 重复识别同一文本：重显缓存译文（与划词去重策略一致）
            self._popup.show_cached(self._last_result)
            return
        # 失败不记录去重文本，允许立即重试同一段截图
        self._pending_text = stripped

        # 识别成功，进入共用翻译管线
        self._popup.show_loading()
        translator = Translator(self._backend, self._config)
        self._worker = TranslateWorker(translator, stripped)
        self._worker.token.connect(self._popup.append_token)
        self._worker.succeeded.connect(self._on_translate_success)
        self._worker.failed.connect(self._on_translate_failed)
        self._worker.retry.connect(self._on_retry)
        track_worker(self, "_worker", self._worker)
        self._worker.start()

    def _on_ocr_failed(self, message: str) -> None:
        self._ocr_worker = None
        if self._popup:
            self._popup.show_error(message)

    # ---------- 翻译回调 ----------
    def _on_translate_success(self, result: str) -> None:
        self._last_text = self._pending_text
        self._last_result = result
        if self._popup:
            self._popup.set_translation(result)

    def _on_translate_failed(self, message: str) -> None:
        self._last_text = ""
        self._last_result = ""
        if self._popup:
            self._popup.show_error(message)

    def _on_retry(self) -> None:
        if self._popup:
            self._popup.show_loading()

    # ---------- 去重缓存 ----------
    def invalidate_last_text(self) -> None:
        """清空去重缓存，设置/术语表/目标语言变化后强制重新翻译。"""
        self._last_text = ""
        self._last_result = ""

    # ---------- 切换模型 ----------
    def rebuild_backend(self) -> None:
        """切换模型后重建后端并重新注册热键。"""
        self.stop()
        self._close_backend()
        self._backend = self._make_backend()
        self.start()

    # ---------- 清理 ----------
    def _cancel_workers(self) -> None:
        """取消进行中的 OCR / 翻译 worker。

        先断信号再等待：RapidOCR 为进程内推理、不可外部中断，
        断信号保证迟到结果不会污染新一轮流程；僵尸包装器免疫
        与 SelectionTranslator._cancel_worker 相同。
        """
        w = self._ocr_worker
        self._ocr_worker = None
        if w is not None:
            with contextlib.suppress(RuntimeError, TypeError):
                w.succeeded.disconnect()
                w.failed.disconnect()
            with contextlib.suppress(RuntimeError):  # C++ 对象已删除
                w.wait(2000)

        w2 = self._worker
        self._worker = None
        if w2 is None:
            return
        try:
            w2.request_stop()
            w2.wait(2000)
        except RuntimeError:
            return
        try:
            for sig in (w2.token, w2.succeeded, w2.failed, w2.retry):
                sig.disconnect()
        except (TypeError, RuntimeError):
            pass

    def shutdown(self) -> None:
        self.stop()
        if self._popup:
            self._popup.hide()
            self._popup = None
        self._close_backend()
