"""划词翻译悬浮窗。

无边框置顶小窗，跟随鼠标定位（多屏边界翻转），流式显示译文，
失焦或超时后自动隐藏。译文区可选中复制，内容过长时滚动显示。
支持拖动和关闭按钮。
"""

from __future__ import annotations

import time

from PyQt6.QtCore import QEvent, QPoint, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QCursor, QGuiApplication
from PyQt6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
)

POPUP_WIDTH = 380
# 取不到屏幕信息时的最大高度兜底（正常取所在屏幕可用高度的 1/4）
POPUP_MAX_HEIGHT = 360
POPUP_MARGIN = 12
# 高度计算时为滚动预留的宽度，保证滚动条出现时文本宽度不变、不重排
_SCROLLBAR_RESERVE = 10
# 窗口装饰高度：上边距(10) + 下边距(12) + 间距(6) + 标题栏(20)
_CHROME_HEIGHT = 48
# 最小窗口高度，避免空内容时坍缩
_MIN_HEIGHT = 60
# 最大高度下限：超小屏幕上 1/4 屏高可能只剩百来像素，
# 兜住避免窗口退化成一条缝（仍远低于任何正常屏幕的 1/4）
_MIN_MAX_HEIGHT = 180
# 等待首个 token 超过此时长后，提示"后端可能在加载模型"
_SLOW_HINT_MS = 8000


class TranslationPopup(QFrame):
    """无边框置顶悬浮窗，流式展示译文，长内容滚动显示。"""

    # 用户点 ✕：请求取消当前翻译（隐藏窗口由本类自行处理）
    close_requested = pyqtSignal()

    # 用户点 ⟳：请求取消当前翻译并用同一文本重新翻译
    retry_requested = pyqtSignal()

    def __init__(self, auto_hide_ms: int = 0, parent=None):
        super().__init__(
            parent,
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowStaysOnTopHint,
        )
        self.setObjectName("TramPopup")
        self._auto_hide_ms = auto_hide_ms
        self._drag_pos: QPoint | None = None
        self._loading_text = "翻译中…"  # 当前 loading 占位文案（OCR 用"识别中…"）
        self._load_started = 0.0  # show_loading 时刻，慢提示据此计算等待秒数

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.hide)

        # 慢响应提示：翻译开始后 8s 无 token 先给出提示，
        # 随后每秒刷新已等待秒数（后端加载模型可长达数分钟，
        # 静止不变的文案会让用户误以为界面冻住）
        self._slow_hint_timer = QTimer(self)
        self._slow_hint_timer.setSingleShot(True)
        self._slow_hint_timer.timeout.connect(self._show_slow_hint)
        self._elapsed_timer = QTimer(self)
        self._elapsed_timer.timeout.connect(self._update_slow_hint)

        self._build_ui()
        self._apply_style()

    def _build_ui(self) -> None:
        self.setFixedWidth(POPUP_WIDTH)
        # 高度完全由 _adjust_height 通过 setFixedHeight 控制：
        # 内容少时随文本增长，到达屏幕 1/4 高度上限后由
        # QScrollArea 滚动显示溢出内容（见 _adjust_height）。
        # 不在构造时预设 setMaximumHeight，避免与 setFixedHeight
        # 产生 MINMAXINFO 约束冲突。
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

        # 标题栏：拖动提示 + 关闭按钮，右对齐
        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(0)

        drag_hint = QLabel("点击这里拖动浮窗")
        drag_hint.setStyleSheet("color: #5a6270; font-size: 11px;")
        drag_hint.setCursor(Qt.CursorShape.SizeAllCursor)

        close_btn = QPushButton("✕")  # ✕
        close_btn.setFixedSize(20, 20)
        close_btn.setFlat(True)
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.setObjectName("TramClose")
        close_btn.clicked.connect(self._on_close_clicked)

        # 重试按钮：翻译相关状态（加载/流式/完成/缓存展示/失败）常驻显示，
        # 仅 OCR 识别阶段与无文本可重试的死路错误隐藏
        retry_btn = QPushButton("⟳")
        retry_btn.setFixedSize(20, 20)
        retry_btn.setFlat(True)
        retry_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        retry_btn.setObjectName("TramRetry")
        retry_btn.setToolTip("重新翻译")
        retry_btn.clicked.connect(self._on_retry_clicked)
        retry_btn.hide()
        self._retry_btn = retry_btn

        header.addWidget(drag_hint)
        header.addStretch()
        header.addWidget(retry_btn)
        header.addWidget(close_btn)

        # 译文：QLabel 放入 QScrollArea，内容过长时垂直滚动
        self._target_label = QLabel()
        self._target_label.setWordWrap(True)
        self._target_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self._target_label.setAlignment(
            Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft
        )
        self._target_label.setProperty("role", "target")

        self._scroll = QScrollArea()
        self._scroll.setWidget(self._target_label)
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self._scroll.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        vbar = self._scroll.verticalScrollBar()
        assert vbar is not None  # QScrollArea 垂直滚动条总是存在
        self._vbar = vbar

        lay = QVBoxLayout(self)
        lay.setContentsMargins(POPUP_MARGIN, 10, POPUP_MARGIN, POPUP_MARGIN)
        lay.setSpacing(6)
        lay.addLayout(header)
        lay.addWidget(self._scroll)

    def _apply_style(self) -> None:
        self.setStyleSheet(
            "#TramPopup { background: rgba(38, 42, 52, 240);"
            "  border: 1px solid rgba(255,255,255,40);"
            "  border-radius: 10px; }"
            "#TramClose { color: #788090; font-size: 13px; border: none; }"
            "#TramClose:hover { color: #e8e8ec; }"
            "#TramRetry { color: #788090; font-size: 13px; border: none; }"
            "#TramRetry:hover { color: #e8e8ec; }"
            "QLabel { color: #e8e8ec; font-size: 13px; }"
            'QLabel[role="target"] { color: #f5f6f8; font-size: 14px; }'
            "QScrollArea { border: none; background: transparent; }"
            "QScrollArea > QWidget > QWidget { background: transparent; }"
            # 细滚动条，透明轨道
            "QScrollBar:vertical { background: transparent;"
            "  width: 8px; margin: 2px 1px 2px 0; }"
            "QScrollBar::handle:vertical { background: rgba(255,255,255,60);"
            "  border-radius: 3px; min-height: 24px; }"
            "QScrollBar::handle:vertical:hover { background: rgba(255,255,255,100); }"
            "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }"
            "QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {"
            "  background: transparent; }"
        )

    # ---------- 内容 ----------
    def show_loading(self, label: str = "翻译中…", can_retry: bool = False) -> None:
        """进入加载态。can_retry：是否允许重试（翻译阶段）。

        OCR 识别阶段不可重试（RapidOCR 进程内推理无法中断，
        重新识别直接再按热键框选即可），翻译阶段可中断重发。
        """
        self._loading_text = label  # append_token/慢提示据此识别占位文案
        self._load_started = time.monotonic()
        self._retry_btn.setVisible(can_retry)
        self._target_label.setStyleSheet("")  # 清除错误样式
        self._target_label.setText(label)
        self._scroll_to_top()
        self._show()
        # 后端加载模型等场景首个 token 会很慢，超时给出提示避免像卡死
        self._slow_hint_timer.start(_SLOW_HINT_MS)

    def append_token(self, token: str) -> None:
        """流式追加译文。仅当用户未向上翻看时自动跟随到底部。"""
        self._stop_loading_hints()
        bar = self._vbar
        stick_to_bottom = bar.value() >= bar.maximum() - 4

        cur = self._target_label.text()
        # startswith：兼容"翻译中…"（或 OCR 的"识别中…"）与慢响应提示文案
        if cur.startswith(self._loading_text):
            cur = ""
        self._target_label.setText(cur + token)
        self._adjust_height()
        if stick_to_bottom:
            # 延迟到布局完成后滚动，确保滚动条 range 已按新内容更新
            QTimer.singleShot(0, self._scroll_to_bottom)

    def set_translation(self, text: str) -> None:
        self._stop_loading_hints()
        bar = self._vbar
        stick_to_bottom = bar.value() >= bar.maximum() - 4
        self._target_label.setText(text)
        self._adjust_height()
        if stick_to_bottom:
            QTimer.singleShot(0, self._scroll_to_bottom)

    def show_error(self, message: str, can_retry: bool = True) -> None:
        """错误态。can_retry：翻译类错误默认允许重试；OCR 识别失败 /
        "仍在结束中"等死路错误传 False（重试无意义或无待重试文本）。"""
        self._stop_loading_hints()
        self._retry_btn.setVisible(can_retry)
        self._target_label.setText(f"❌ {message}")
        self._target_label.setStyleSheet("color: #e0a3a3;")
        self._scroll_to_top()
        self._adjust_height()
        self._show()

    def _show_slow_hint(self) -> None:
        """等待首个 token 超时：提示后端可能在加载模型，开始每秒计时。"""
        if not self._target_label.text().startswith(self._loading_text):
            return  # 已有译文/报错则不覆盖
        self._update_slow_hint()
        self._elapsed_timer.start(1000)

    def _update_slow_hint(self) -> None:
        """每秒刷新等待秒数：让用户确认仍在等待而非界面冻住。"""
        if not self._target_label.text().startswith(self._loading_text):
            self._elapsed_timer.stop()  # 状态已被 token/错误接管，兜底停表
            return
        elapsed = max(int(time.monotonic() - self._load_started), 1)
        self._target_label.setText(
            f"{self._loading_text}（后端可能在加载模型，已等待 {elapsed}s）"
        )
        self._adjust_height()

    def _stop_loading_hints(self) -> None:
        """停止慢提示与等待计时（有产出/终态时调用）。"""
        self._slow_hint_timer.stop()
        self._elapsed_timer.stop()

    def _on_close_clicked(self) -> None:
        """✕：通知外部取消翻译，并隐藏窗口。"""
        self._stop_loading_hints()
        self._retry_btn.hide()
        self.close_requested.emit()
        self.hide()

    def _on_retry_clicked(self) -> None:
        """⟳：通知外部取消当前翻译并用同一文本重新翻译。

        不改本地状态：外部随即调用 show_loading 重置加载态。
        """
        self.retry_requested.emit()

    def show_capturing(self) -> None:
        """热键触发后立即展示最小窗口，提示"正在捕获"。

        关键：不调用 activateWindow()/setFocus()，否则弹窗抢走焦点
        会导致后续 SendInput(Ctrl+C) 发到弹窗而非用户选中文本的窗口。
        调用 processEvents 强制刷新一次，确保窗口在 grab_selection
        阻塞主线程前已经渲染。
        """
        self._target_label.setStyleSheet("")  # 清除错误样式
        self._target_label.setText("正在捕获…")
        self._scroll_to_top()
        self._adjust_height()
        self._position_at_cursor()
        self.show()
        # 仅提升 Z 序，不抢焦点
        self.raise_()
        # 强制刷新一次 UI，让"正在捕获"在 grab_selection 阻塞前渲染
        QApplication.processEvents()

    def show_cached(self, text: str) -> None:
        """直接展示缓存的译文（重复触发同一文本，无需重新翻译）。

        与完整翻译流程的终态一致：可复制、可滚动、失焦自动隐藏。
        显式显示重试按钮：缓存路径不经过 show_loading（每次热键都是
        全新浮窗实例，按钮初始隐藏），用户可点 ⟳ 强制重新请求翻译。
        """
        self._stop_loading_hints()
        self._retry_btn.show()
        self._target_label.setStyleSheet("")  # 清除错误样式
        self._target_label.setText(text)
        self._scroll_to_top()
        self._show()

    def fade_out(self, text: str = "未检测到选中文本") -> None:
        """短暂显示提示文案后自动隐藏（捕获失败/无识别结果）。"""
        self._stop_loading_hints()
        self._retry_btn.hide()
        self._target_label.setText(text)
        self._target_label.setStyleSheet("color: #9aa0ac;")
        self._scroll_to_top()
        self._adjust_height()
        self._position_at_cursor()
        self.show()
        self.raise_()
        QTimer.singleShot(800, self.hide)

    # ---------- 显示/隐藏 ----------
    def _show(self) -> None:
        self._adjust_height()
        self._position_at_cursor()
        self.show()
        self.raise_()
        self.activateWindow()
        self.setFocus()
        if self._auto_hide_ms > 0:
            self._timer.start(self._auto_hide_ms)
        else:
            self._timer.stop()

    def _max_window_height(self) -> int:
        """最大窗口高度：浮窗所在屏幕可用高度的 1/4。

        已显示时以窗口所在屏幕为准（流式输出中途换屏/拖动后仍准确）；
        未显示时按光标所在屏幕预估（与 _position_at_cursor 同一块屏）。
        """
        screen = self.screen() if self.isVisible() else None
        if screen is None:
            screen = (
                QGuiApplication.screenAt(QCursor.pos())
                or QGuiApplication.primaryScreen()
            )
        if screen is None:  # 极端情况（无屏幕），用固定兜底
            return POPUP_MAX_HEIGHT
        return max(screen.availableGeometry().height() // 4, _MIN_MAX_HEIGHT)

    def _adjust_height(self) -> None:
        # 可用宽度扣除滚动条预留，保证滚动条出现前后文本不重排
        avail_w = POPUP_WIDTH - 2 * POPUP_MARGIN - _SCROLLBAR_RESERVE
        tgt_h = self._target_label.heightForWidth(avail_w)
        if tgt_h < 18:
            tgt_h = 18  # 单行最小高度，避免空内容时窗口坍缩

        # Chrome: top margin(10) + bottom margin(12) + spacing(6)
        # + header(close btn 20)
        total = tgt_h + _CHROME_HEIGHT
        # 上限内随内容增长；触顶后 QScrollArea 出现滚动条展示溢出
        self.setFixedHeight(min(max(total, _MIN_HEIGHT), self._max_window_height()))

    def _scroll_to_top(self) -> None:
        self._vbar.setValue(0)

    def _scroll_to_bottom(self) -> None:
        self._vbar.setValue(self._vbar.maximum())

    def _position_at_cursor(self) -> None:
        cursor = QCursor.pos()
        screen = QGuiApplication.screenAt(cursor) or QGuiApplication.primaryScreen()
        if screen is None:  # 极端情况（无屏幕），跳过定位用默认位置
            return
        geo = screen.availableGeometry()
        w = self.width()
        h = self.height()

        x = cursor.x() + 16
        y = cursor.y() + 20
        if x + w > geo.right():
            x = cursor.x() - w - 16
        if y + h > geo.bottom():
            y = cursor.y() - h - 20
        x = max(geo.left(), min(x, geo.right() - w))
        y = max(geo.top(), min(y, geo.bottom() - h))
        self.move(x, y)

    # ---------- 失焦隐藏 ----------
    def changeEvent(self, event) -> None:
        if self._auto_hide_ms <= 0 and event.type() == QEvent.Type.WindowDeactivate:
            self._stop_loading_hints()
            self.hide()
        super().changeEvent(event)

    # ---------- 拖动 ----------
    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._drag_pos is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_pos)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        self._drag_pos = None
        super().mouseReleaseEvent(event)
