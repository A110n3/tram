"""OCR 识图翻译编排器。

公共骨架（后端/热键生命周期、去重缓存、翻译管线）见 base_translator；
本类只保留 OCR 特有流程：热键 -> 截主屏 -> 全屏覆盖层框选 -> 裁剪
-> OCRWorker 识别 -> 复用公共翻译管线 -> TranslationPopup 展示。

翻译管线（loading/流式/错误/缓存重显）与划词翻译完全共用。
"""

from __future__ import annotations

import contextlib
import logging

from PyQt6.QtCore import QRect
from PyQt6.QtGui import QGuiApplication, QPixmap

from ..config import get_default
from ..core.ocr import is_rapidocr_available, pixmap_to_png
from .base_translator import BaseHotkeyTranslator
from .region_overlay import RegionOverlay
from .worker import OCRWorker
from .worker_util import track_worker

logger = logging.getLogger(__name__)

# OCR 热键的 RegisterHotKey id，与划词热键（id=1）区分
OCR_HOTKEY_ID = 2


class OCRTranslator(BaseHotkeyTranslator):
    """管理 OCR 识图翻译全流程：热键 -> 框选 -> 识别 -> 翻译 -> 悬浮窗。"""

    section = "ocr"
    service_name = "OCR"
    hotkey_id = OCR_HOTKEY_ID

    # 热键时刻截取的冻结截图：覆盖层背景与裁剪必须使用同一张
    _screenshot: QPixmap | None = None
    _overlay: RegionOverlay | None = None
    _ocr_worker: OCRWorker | None = None

    # ---------- 热键处理 ----------
    def _on_hotkey(self) -> None:
        # 1. 清理进行中的 worker 与残留覆盖层；旧翻译线程未死透则拒绝本轮
        if not self._cancel_workers():
            if self._popup:
                self._popup.show_error("上一次翻译仍在结束中，请稍后重试")
            return
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

    # ---------- 覆盖层 ----------
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

    def _on_stopping(self) -> None:
        """stop 钩子：停止时关闭选区覆盖层。"""
        self._close_overlay()

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
        popup = self._new_popup()
        popup.show_loading("识别中…")

        languages = self._config.get("ocr", {}).get(
            "languages", get_default("ocr", "languages")
        )
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
        min_chars = self._config.get("ocr", {}).get(
            "min_chars", get_default("ocr", "min_chars")
        )
        if len(stripped) < max(int(min_chars), 1):
            self._popup.fade_out("未识别到文字")
            return
        if self._try_show_cached(stripped):
            return
        # 识别成功，进入公共翻译管线
        self._begin_translation(stripped)

    def _on_ocr_failed(self, message: str) -> None:
        self._ocr_worker = None
        if self._popup:
            self._popup.show_error(message)

    # ---------- worker 取消 ----------
    def _cancel_workers(self) -> bool:
        """扩展基类：先取消 OCR 识别 worker，再取消翻译 worker。

        先断信号再等待：RapidOCR 为进程内推理、不可外部中断，
        断信号保证迟到结果不会污染新一轮流程。
        """
        w = self._ocr_worker
        self._ocr_worker = None
        ocr_ok = True
        if w is not None:
            with contextlib.suppress(RuntimeError, TypeError):
                w.succeeded.disconnect()
                w.failed.disconnect()
            with contextlib.suppress(RuntimeError):  # C++ 对象已删除
                if not w.wait(2000):
                    logger.warning("OCR 线程 2s 未退出")
                    ocr_ok = False
        return super()._cancel_workers() and ocr_ok
