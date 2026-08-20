"""区域实时监控翻译编排器。

复用 BaseHotkeyTranslator 的热键/后端生命周期骨架，会话流程为：
热键 -> 冻结截屏 + RegionOverlay 框选 -> MonitorWorker 后台循环
（截图/帧差/OCR/查重漏斗）-> 丢旧保新提交 TranslateWorker ->
MonitorWindow 流式展示。

「丢旧保新」：监控期间同一时刻只允许一个翻译请求在途，新字幕到
来时取消未完成的旧请求、提交新文本（用户只关心最新字幕）。
"""

from __future__ import annotations

import contextlib
import logging

from PyQt6.QtCore import QRect
from PyQt6.QtGui import QGuiApplication, QPixmap

from ..config import get_default
from ..core.monitor import MonitorParams
from ..core.ocr import is_rapidocr_available
from .base_translator import BaseHotkeyTranslator
from .monitor_window import MonitorWindow
from .region_overlay import RegionOverlay
from .worker import MonitorWorker, TranslateWorker
from .worker_util import track_worker

logger = logging.getLogger(__name__)

# 监控热键的 RegisterHotKey id（划词=1，OCR=2）
MONITOR_HOTKEY_ID = 3


class MonitorTranslator(BaseHotkeyTranslator):
    """管理区域监控全流程：热键 -> 框选 -> 监控漏斗 -> 丢旧保新翻译 -> 小窗。"""

    section = "monitor"
    service_name = "监控"
    hotkey_id = MONITOR_HOTKEY_ID

    _screenshot: QPixmap | None = None
    _overlay: RegionOverlay | None = None
    _monitor_worker: MonitorWorker | None = None
    _window: MonitorWindow | None = None

    # ---------- 热键处理：会话切换 ----------
    def _on_hotkey(self) -> None:
        if self._monitor_worker is not None or self._overlay is not None:
            self.stop_session()
            return
        if not self._cancel_workers():
            self.hotkey_status.emit(False, "上一次翻译仍在结束中，请稍后重试")
            return
        if not is_rapidocr_available():
            self.hotkey_status.emit(
                False, 'OCR 引擎未安装，请运行 pip install "tram[ocr]"'
            )
            return
        self._close_overlay()
        # 冻结截屏（必须在覆盖层显示之前，避免把遮罩截进去）
        screen = QGuiApplication.primaryScreen()
        if screen is None:
            return
        self._screenshot = screen.grabWindow(0)  # type: ignore[arg-type]
        overlay = RegionOverlay(self._screenshot)
        self._overlay = overlay
        overlay.region_selected.connect(self._on_region)
        overlay.cancelled.connect(self._close_overlay_slot)
        overlay.showFullScreen()
        overlay.raise_()
        overlay.activateWindow()

    # ---------- 覆盖层 ----------
    def _close_overlay(self) -> None:
        """主动关闭覆盖层（stop / 新热键路径）。"""
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

    # ---------- 监控会话 ----------
    def _on_region(self, region: QRect) -> None:
        self._overlay = None
        screenshot = self._screenshot
        self._screenshot = None
        if screenshot is None or screenshot.isNull():
            return

        # 选区为覆盖层本地逻辑坐标，截图 pixmap 为设备像素：
        # 按 devicePixelRatio 换算为物理像素 bbox（ImageGrab 坐标系）
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
        bbox = (src.x(), src.y(), src.x() + src.width(), src.y() + src.height())

        cfg = self._config.get("monitor", {})
        params = MonitorParams(
            interval_ms=int(cfg.get("interval_ms", get_default("monitor", "interval_ms"))),
            diff_threshold=float(
                cfg.get("diff_threshold", get_default("monitor", "diff_threshold"))
            ),
            similarity_threshold=float(
                cfg.get(
                    "similarity_threshold", get_default("monitor", "similarity_threshold")
                )
            ),
            history_size=int(
                cfg.get("history_size", get_default("monitor", "history_size"))
            ),
            min_chars=int(cfg.get("min_chars", get_default("monitor", "min_chars"))),
        )

        window = MonitorWindow(history_size=params.history_size)
        self._window = window
        window.closed.connect(self._on_window_closed)
        window.show()
        window.raise_()

        w = MonitorWorker(bbox, params)
        self._monitor_worker = w
        w.new_text.connect(self._on_new_text)
        w.failed.connect(self._on_monitor_failed)
        track_worker(self, "_monitor_worker", w)
        w.start()

    def _on_monitor_failed(self, message: str) -> None:
        """监控循环异常（OCR 引擎/截图失败）：结束会话并提示。"""
        self.stop_session()
        self.hotkey_status.emit(False, f"监控已停止：{message[:120]}")

    def _on_window_closed(self) -> None:
        """用户关闭监控小窗：结束会话。"""
        self._window = None
        self.stop_session()

    def stop_session(self) -> None:
        """停止监控会话：停监控线程、取消翻译、关窗（热键切换/异常/退出共用）。"""
        self._close_overlay()
        w = self._monitor_worker
        self._monitor_worker = None
        if w is not None:
            with contextlib.suppress(RuntimeError, TypeError):
                w.new_text.disconnect()
                w.failed.disconnect()
            try:
                w.request_stop()
                # OCR 单帧推理有界（<2s），等待退出避免销毁运行中的 QThread
                if w.isRunning() and not w.wait(2000):
                    logger.warning("监控线程 2s 未退出")
            except RuntimeError:
                pass  # C++ 对象已删除（僵尸包装器）
        self._cancel_workers()
        win = self._window
        self._window = None
        if win is not None:
            with contextlib.suppress(RuntimeError):
                win.closed.disconnect()
                win.close()
                win.deleteLater()

    def _on_stopping(self) -> None:
        """stop 钩子（服务关闭）：同时结束进行中的监控会话。"""
        self.stop_session()

    # ---------- 丢旧保新翻译 ----------
    def _on_new_text(self, text: str) -> None:
        """漏斗产出新字幕：取消在途翻译，提交最新文本。"""
        # 先取消旧请求并等它退出：backend 连接与取消事件不支持并发复用
        if not self._cancel_workers():
            logger.warning("旧翻译线程未退出，丢弃本条字幕")
            return
        window = self._window
        if window is None:
            return  # 小窗已被用户关闭（会话停止信号在途）
        window.begin_translation(text)
        self._pending_text = text
        w = TranslateWorker(self._backend, self._config, text)
        self._worker = w
        w.token.connect(window.append_token)
        w.succeeded.connect(self._on_translate_success)
        w.failed.connect(self._on_translate_failed)
        track_worker(self, "_worker", w)
        w.start()

    def _on_translate_success(self, result: str) -> None:
        self._last_text = self._pending_text
        self._last_result = result
        window = self._window
        if window is not None:
            window.set_translation(result)

    def _on_translate_failed(self, message: str) -> None:
        self._last_text = ""
        self._last_result = ""
        window = self._window
        if window is not None:
            window.show_error(message)
