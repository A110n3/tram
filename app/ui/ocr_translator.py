"""OCR 识图翻译编排器。

公共骨架（后端/热键生命周期、去重缓存、翻译管线）见 base_translator；
本类只保留 OCR 特有流程：热键 -> mss 截主屏 -> 全屏覆盖层框选 -> 裁剪
-> OCRWorker 识别 -> 复用公共翻译管线 -> TranslationPopup 展示。

截图使用 mss 库（GDI 后端），直接出 BGR ndarray，跳过 PNG 编解码；
同时转一份 QPixmap 给覆盖层做背景显示。

翻译管线（loading/流式/错误/缓存重显）与划词翻译完全共用。
"""

from __future__ import annotations

import contextlib
import logging
from typing import Any

from PyQt6.QtCore import QRect
from PyQt6.QtGui import QGuiApplication, QPixmap

from ..config import get_default
from ..core.ocr import (
    capture_primary_screen,
    is_rapidocr_available,
    ndarray_to_qpixmap,
)
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

    # 热键时刻截取的冻结截图（BGR ndarray）：裁剪与识别使用
    _screenshot_bgr: Any = None  # np.ndarray | None
    # 覆盖层用的 QPixmap（从同一张 ndarray 转换，保证背景与裁剪一致）
    _screenshot_pixmap: QPixmap | None = None
    # 主屏 monitor info（物理坐标：left/top/width/height）
    _monitor_info: dict | None = None
    _overlay: RegionOverlay | None = None
    _ocr_worker: OCRWorker | None = None

    # ---------- 热键处理 ----------
    def _on_hotkey(self) -> None:
        # 1. 清理进行中的 worker 与残留覆盖层；旧翻译线程未死透则拒绝本轮
        if not self._cancel_workers():
            if self._popup:
                self._popup.show_error(
                    "上一次翻译仍在结束中，请稍后重试", can_retry=False
                )
            return
        if self._popup:
            self._popup.hide()
            self._popup = None
        self._close_overlay()

        # 2. OCR 引擎缺失：发出状态消息（主窗口统一写日志），不弹覆盖层
        if not is_rapidocr_available():
            self.hotkey_status.emit(
                False,
                'OCR 引擎未安装，请运行 pip install "tram[ocr]"',
            )
            return

        # 3. 截主屏（必须在覆盖层显示之前，避免把遮罩截进去）
        #    mss 直接出 BGR ndarray，同时转一份 QPixmap 给覆盖层背景
        try:
            bgr, monitor = capture_primary_screen()
        except Exception as e:
            logger.warning("OCR 截图失败: %s", e, exc_info=True)
            self.hotkey_status.emit(False, f"截图失败：{e}")
            return
        self._screenshot_bgr = bgr
        self._monitor_info = monitor
        self._screenshot_pixmap = ndarray_to_qpixmap(bgr)
        if self._screenshot_pixmap.isNull():
            self.hotkey_status.emit(False, "截图转 QPixmap 失败")
            return

        # 4. 弹全屏选区覆盖层，结果由 _on_region/_close_overlay_slot 接收
        overlay = RegionOverlay(self._screenshot_pixmap)
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
        self._screenshot_bgr = None
        self._screenshot_pixmap = None
        self._monitor_info = None
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
        self._screenshot_bgr = None
        self._screenshot_pixmap = None
        self._monitor_info = None

    def _on_stopping(self) -> None:
        """stop 钩子：停止时关闭选区覆盖层。"""
        self._close_overlay()

    # ---------- 识别 ----------
    def _on_region(self, region: QRect) -> None:
        self._overlay = None  # 覆盖层已自行 close + deleteLater
        bgr = self._screenshot_bgr
        monitor = self._monitor_info
        self._screenshot_bgr = None
        self._screenshot_pixmap = None
        self._monitor_info = None
        if bgr is None or monitor is None:
            return

        # 选区是覆盖层的逻辑坐标（逻辑像素），mss 截图是物理像素。
        # 覆盖层 geometry 与屏幕 geometry 一致，用 devicePixelRatio 换算
        screen = QGuiApplication.primaryScreen()
        dpr = screen.devicePixelRatio() if screen else 1.0
        x = round(region.x() * dpr)
        y = round(region.y() * dpr)
        w = round(region.width() * dpr)
        h = round(region.height() * dpr)

        # 边界裁剪：限制在截图尺寸内
        img_h, img_w = bgr.shape[:2]
        x = max(0, min(x, img_w))
        y = max(0, min(y, img_h))
        w = max(0, min(w, img_w - x))
        h = max(0, min(h, img_h - y))
        if w == 0 or h == 0:
            return

        # 从 ndarray 裁剪 + 放大（小选区），不走 PNG 编解码，速度更快
        from ..core.ocr import crop_and_upscale

        try:
            cropped = crop_and_upscale(bgr, x, y, w, h)
        except Exception as e:
            logger.warning("OCR 裁剪失败: %s", e, exc_info=True)
            return

        # 弹窗在框选结束（鼠标释放）后出现，跟随当前光标定位
        popup = self._new_popup()
        popup.show_loading("识别中…")

        languages = self._config.get("ocr", {}).get(
            "languages", get_default("ocr", "languages")
        )
        worker = OCRWorker(cropped, languages, use_ndarray=True)
        self._ocr_worker = worker
        worker.succeeded.connect(self._on_ocr_ok)
        worker.failed.connect(self._on_ocr_failed)
        track_worker(self, "_ocr_worker", worker)
        worker.start()

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
            # OCR 识别失败无待重试文本（识别阶段不可中断重发）
            self._popup.show_error(message, can_retry=False)

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
