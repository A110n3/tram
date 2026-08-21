"""区域实时监控翻译编排器。

复用 BaseHotkeyTranslator 的热键/后端生命周期骨架，会话流程为：
热键 -> 冻结截屏 + RegionOverlay 框选 -> MonitorWorker 后台循环
（截图/帧差/OCR/查重漏斗）-> 有界队列串行提交 TranslateWorker ->
MonitorWindow 流式展示。

「有界队列」：同一时刻只允许一个翻译请求在途（backend 连接不支持
并发复用）。正在翻译的请求不打断——中途取消会浪费已算掉的部分，
且取消等待本身占用时间；新字幕进入 FIFO 队列排队，队列满时丢弃
最旧的等待项（越新越贴近当前画面）。字幕切换持续快于翻译速度时，
译文最多落后「队列长度 × 单条翻译耗时」，这是本地模型吞吐受限下
"少丢句"与"低延迟"之间的折中。
"""

from __future__ import annotations

import contextlib
import logging
from collections import deque

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
    """管理区域监控全流程：热键 -> 框选 -> 监控漏斗 -> 有界队列翻译 -> 小窗。"""

    section = "monitor"
    service_name = "监控"
    hotkey_id = MONITOR_HOTKEY_ID

    _screenshot: QPixmap | None = None
    _overlay: RegionOverlay | None = None
    _monitor_worker: MonitorWorker | None = None
    _window: MonitorWindow | None = None
    # 翻译排队：在途一条 + 队列等待若干（会话开始时重建，见 _on_region）
    _queue: deque[str] = deque()
    _queue_max: int = 1
    _dropped: int = 0  # 本会话因队列满被丢弃的等待条数（小窗状态栏提示）

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
            debounce=int(cfg.get("debounce", get_default("monitor", "debounce"))),
            history_size=int(
                cfg.get("history_size", get_default("monitor", "history_size"))
            ),
            pause_on_cursor=bool(
                cfg.get("pause_on_cursor", get_default("monitor", "pause_on_cursor"))
            ),
            min_chars=int(cfg.get("min_chars", get_default("monitor", "min_chars"))),
        )
        # 每次会话重建排队状态：残留的旧字幕不应带入新会话
        self._queue = deque()
        self._dropped = 0
        self._queue_max = max(
            int(cfg.get("queue_size", get_default("monitor", "queue_size"))), 1
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
        """停止监控会话：停监控线程、取消翻译、清队列、关窗。

        热键切换/异常/退出共用。排队中的字幕随会话作废，不带入
        下次会话（_on_region 也会重建队列）。
        """
        self._close_overlay()
        self._queue.clear()
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

    # ---------- 有界队列串行翻译 ----------
    def _on_new_text(self, text: str) -> None:
        """漏斗产出新字幕：空闲则立即翻译，在途则排队（满则丢最旧）。

        不打断在途翻译：取消会浪费已算掉的部分，短字幕翻完往往比
        取消重启更快。队列满时丢最旧的等待项——快对话里越新的
        句子越贴近当前画面，丢旧损失最小。
        """
        if self._window is None:
            return  # 小窗已被用户关闭（会话停止信号在途）
        if self._worker is not None:
            if len(self._queue) >= self._queue_max:
                self._queue.popleft()
                self._dropped += 1
                logger.info("翻译队列已满，丢弃最旧等待字幕（累计 %d 条）", self._dropped)
            self._queue.append(text)
        else:
            self._start_translation(text)
        self._update_status()

    def _start_translation(self, text: str) -> None:
        """启动一条翻译（调用方保证此刻无在途 worker）。"""
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
        # 须在 track_worker 之后连接：finished 先触发 forget_worker 清
        # 引用（队列已空时），_update_status 才能看到 _worker=None，
        # 状态从「翻译中」回到「监控中」；排队续翻时身份校验不清引用，
        # 状态正确显示在途的新 worker
        w.finished.connect(self._update_status)
        w.start()

    def _next_from_queue(self) -> None:
        """当前翻译结束：取队首继续；backend 不支持并发，必须串行。"""
        if self._queue:
            self._start_translation(self._queue.popleft())
        self._update_status()

    def _update_status(self) -> None:
        """小窗标题栏状态：翻译进度 + 排队深度 + 累计丢弃条数。"""
        window = self._window
        if window is None:
            return
        queued = len(self._queue)
        if self._worker is not None:
            status = "翻译中" if queued == 0 else f"翻译中（{queued} 条等待）"
        else:
            status = "监控中"
        if self._dropped:
            status += f" · 已丢弃 {self._dropped} 条"
        window.show_status(status)

    def _on_translate_success(self, result: str) -> None:
        self._last_text = self._pending_text
        self._last_result = result
        window = self._window
        if window is not None:
            window.set_translation(result)
        self._next_from_queue()

    def _on_translate_failed(self, message: str) -> None:
        self._last_text = ""
        self._last_result = ""
        window = self._window
        if window is not None:
            window.show_error(message)
        # 单条失败不阻塞队列：继续翻下一条，错误提示会被新翻译覆盖
        self._next_from_queue()
