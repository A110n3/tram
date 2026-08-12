"""划词翻译悬浮窗。

无边框置顶小窗，跟随鼠标定位（多屏边界翻转），流式显示译文，
失焦或超时后自动隐藏。译文区可选中复制。
支持拖动（标题栏区域）和关闭按钮。
"""

from __future__ import annotations

from PyQt6.QtCore import QEvent, QPoint, Qt, QTimer
from PyQt6.QtGui import QCursor, QGuiApplication
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
)

POPUP_WIDTH = 380
POPUP_MAX_HEIGHT = 360
POPUP_MARGIN = 12


class TranslationPopup(QFrame):
    """无边框置顶悬浮窗，展示原文与流式译文。"""

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

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.hide)

        self._build_ui()
        self._apply_style()

    def _build_ui(self) -> None:
        self.setFixedWidth(POPUP_WIDTH)
        # 高度完全由 _adjust_height 通过 setFixedHeight 控制，
        # 不在构造时预设 setMaximumHeight / controversial size policy，
        # 避免与 setFixedHeight 产生 MINMAXINFO 约束冲突。
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

        # 标题栏：原文 + 关闭按钮
        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(4)

        self._source_label = QLabel()
        self._source_label.setWordWrap(True)
        self._source_label.setProperty("role", "source")
        self._source_label.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
        )

        close_btn = QPushButton("✕")  # ✕
        close_btn.setFixedSize(20, 20)
        close_btn.setFlat(True)
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.setStyleSheet(
            "#TramClose { color: #788090; font-size: 13px; border: none; }"
            " #TramClose:hover { color: #e8e8ec; }"
        )
        close_btn.setObjectName("TramClose")
        close_btn.clicked.connect(self.hide)

        header.addWidget(self._source_label)
        header.addWidget(close_btn, 0, Qt.AlignmentFlag.AlignTop)

        # 译文
        self._target_label = QLabel()
        self._target_label.setWordWrap(True)
        self._target_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self._target_label.setProperty("role", "target")

        lay = QVBoxLayout(self)
        lay.setContentsMargins(POPUP_MARGIN, 10, POPUP_MARGIN, POPUP_MARGIN)
        lay.setSpacing(6)
        lay.addLayout(header)
        lay.addWidget(self._target_label)

    def _apply_style(self) -> None:
        self.setStyleSheet(
            "#TramPopup { background: rgba(38, 42, 52, 240);"
            "  border: 1px solid rgba(255,255,255,40);"
            "  border-radius: 10px; }"
            "QLabel { color: #e8e8ec; font-size: 13px; }"
            'QLabel[role="source"] { color: #9aa0ac; font-size: 12px;'
            "  border-bottom: 1px solid rgba(255,255,255,30); padding-bottom: 4px; }"
            'QLabel[role="target"] { color: #f5f6f8; font-size: 14px; }'
        )

    # ---------- 内容 ----------
    def set_source(self, text: str) -> None:
        snippet = text if len(text) <= 120 else text[:120] + "…"
        self._source_label.setText(snippet)
        self._source_label.setVisible(bool(text))

    def show_loading(self, source: str = "") -> None:
        self._target_label.setStyleSheet("")  # 清除错误样式
        if source:
            self.set_source(source)
        self._target_label.setText("翻译中…")
        self._show()

    def append_token(self, token: str) -> None:
        cur = self._target_label.text()
        if cur == "翻译中…":
            cur = ""
        self._target_label.setText(cur + token)
        self._adjust_height()

    def set_translation(self, text: str) -> None:
        self._target_label.setText(text)
        self._adjust_height()

    def show_error(self, message: str) -> None:
        self._target_label.setText(f"❌ {message}")
        self._target_label.setStyleSheet("color: #e0a3a3;")
        self._adjust_height()
        self._show()

    def show_capturing(self) -> None:
        """热键触发后立即展示最小窗口，提示"正在捕获"。

        关键：不调用 activateWindow()/setFocus()，否则弹窗抢走焦点
        会导致后续 SendInput(Ctrl+C) 发到弹窗而非用户选中文本的窗口。
        调用 processEvents 强制刷新一次，确保窗口在 grab_selection
        阻塞主线程前已经渲染。
        """
        from PyQt6.QtWidgets import QApplication

        self._source_label.setVisible(False)
        self._target_label.setStyleSheet("")  # 清除错误样式
        self._target_label.setText("正在捕获…")
        self._adjust_height()
        self._position_at_cursor()
        self.show()
        # 仅提升 Z 序，不抢焦点
        self.raise_()
        # 强制刷新一次 UI，让"正在捕获"在 grab_selection 阻塞前渲染
        QApplication.processEvents()

    def fade_out(self) -> None:
        """捕获失败时短暂提示后自动隐藏。"""
        self._target_label.setText("未检测到选中文本")
        self._target_label.setStyleSheet("color: #9aa0ac;")
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

    def _adjust_height(self) -> None:
        # QLabel heightForWidth 在未 show 时可能不准确，先确保 widget 已创建
        avail_w = POPUP_WIDTH - 2 * POPUP_MARGIN  # = 356
        src_h = (
            self._source_label.heightForWidth(avail_w)
            if self._source_label.isVisible() and self._source_label.text()
            else 0
        )
        tgt_h = self._target_label.heightForWidth(avail_w)
        if tgt_h < 18:
            tgt_h = 18  # 单行最小高度，避免空内容时窗口坍缩

        # Chrome: top margin(10) + bottom margin(12) + spacing(6)
        # + header(close btn 20 + source label padding ≈ 26)
        chrome = 54
        total = src_h + tgt_h + chrome
        self.setFixedHeight(min(max(total, 60), POPUP_MAX_HEIGHT))

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
