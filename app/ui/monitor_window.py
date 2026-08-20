"""区域监控翻译的独立置顶小窗。

无边框置顶小窗：顶部为可拖拽标题栏 + 关闭按钮，下方为最新译文
（流式显示）与最近 N 条历史（缩小灰字）。与划词悬浮窗
（TranslationPopup）刻意不复用：监控是常驻多轮会话，popup 是
一次性快照展示，交互模型不同。

用户点关闭即视为停止监控（closed 信号由编排器接收）。
"""

from __future__ import annotations

from PyQt6.QtCore import QPoint, Qt, pyqtSignal
from PyQt6.QtGui import QGuiApplication
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

# 历史条目原译文长度超过此值时截断（历史仅作上下文回顾）
_HISTORY_MAX_CHARS = 80


class MonitorWindow(QWidget):
    """实时字幕监控小窗：最新译文 + 历史记录。"""

    closed = pyqtSignal()

    def __init__(self, history_size: int = 5, parent=None):
        super().__init__(
            parent,
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool,
        )
        self._history_size = max(int(history_size), 1)
        self._drag_offset: QPoint | None = None
        # 当前正在流式/展示的原文与译文（用于历史归档）
        self._current_source = ""
        self._current_text = ""

        self.setWindowTitle("Tram 实时字幕")
        self.setMinimumWidth(360)
        self.setMaximumWidth(480)
        self._build_ui()
        self._apply_style()

        # 初始出现在主屏右下角附近（避开屏幕中央的字幕区）
        screen = QGuiApplication.primaryScreen()
        if screen is not None:
            geo = screen.availableGeometry()
            self.move(geo.right() - self.width() - 24, geo.bottom() - 260)

    # ---------- UI ----------
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # 标题栏（拖拽移动）
        bar = QFrame()
        bar.setObjectName("TitleBar")
        bar.setFixedHeight(32)
        bar_lay = QHBoxLayout(bar)
        bar_lay.setContentsMargins(10, 0, 6, 0)
        self._title_label = QLabel("Tram 实时字幕")
        self._title_label.setObjectName("TitleLabel")
        self._status_label = QLabel("监控中")
        self._status_label.setObjectName("StatusLabel")
        close_btn = QToolButton()
        close_btn.setText("x")
        close_btn.setObjectName("CloseButton")
        close_btn.setFixedSize(24, 24)
        close_btn.clicked.connect(self._on_close)
        bar_lay.addWidget(self._title_label)
        bar_lay.addStretch()
        bar_lay.addWidget(self._status_label)
        bar_lay.addWidget(close_btn)
        root.addWidget(bar)

        # 最新译文区（流式追加）
        body = QFrame()
        body.setObjectName("Body")
        body_lay = QVBoxLayout(body)
        body_lay.setContentsMargins(12, 10, 12, 10)
        self._source_label = QLabel("")
        self._source_label.setObjectName("SourceLabel")
        self._source_label.setWordWrap(True)
        self._source_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self._result_label = QLabel("等待字幕…")
        self._result_label.setObjectName("ResultLabel")
        self._result_label.setWordWrap(True)
        self._result_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        body_lay.addWidget(self._source_label)
        body_lay.addWidget(self._result_label)
        root.addWidget(body)

        # 历史区（可滚动）
        self._history_host = QWidget()
        self._history_lay = QVBoxLayout(self._history_host)
        self._history_lay.setContentsMargins(12, 4, 12, 8)
        self._history_lay.setSpacing(6)
        self._history_lay.addStretch()
        scroll = QScrollArea()
        scroll.setObjectName("HistoryScroll")
        scroll.setWidget(self._history_host)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        root.addWidget(scroll)

    def _apply_style(self) -> None:
        self.setStyleSheet(
            """
            MonitorWindow, #Body { background: #1e2128; }
            #TitleBar { background: #171a20; border-bottom: 1px solid #2a2e38; }
            #TitleLabel { color: #a0a8b4; font-size: 12px; font-weight: bold; }
            #StatusLabel { color: #4a9eff; font-size: 11px; }
            #CloseButton {
                color: #788090; font-size: 13px; border: none; border-radius: 4px;
            }
            #CloseButton:hover { background: #e81123; color: white; }
            #SourceLabel {
                color: #788090; font-size: 11px;
                font-family: "Microsoft YaHei", sans-serif;
            }
            #ResultLabel {
                color: #e8eaed; font-size: 14px;
                font-family: "Microsoft YaHei", sans-serif;
            }
            #HistoryScroll { background: #1a1d24; border-top: 1px solid #2a2e38; }
            QLabel[historyEntry="true"] {
                color: #788090; font-size: 11px;
                font-family: "Microsoft YaHei", sans-serif;
            }
            """
        )

    # ---------- 翻译内容更新 ----------
    def begin_translation(self, source: str) -> None:
        """新字幕到来：当前内容归档历史，进入加载态。"""
        self._archive_current()
        self._current_source = source
        self._current_text = ""
        self._source_label.setText(source)
        self._result_label.setText("翻译中…")
        self._adjust_height()

    def append_token(self, token: str) -> None:
        """流式追加译文 token。"""
        self._current_text += token
        self._result_label.setText(self._current_text)
        self._adjust_height()

    def set_translation(self, result: str) -> None:
        """翻译完成：显示最终译文并归档历史。"""
        self._current_text = result
        self._result_label.setText(result)
        self._archive_current()
        self._adjust_height()

    def show_error(self, message: str) -> None:
        """翻译失败：错误态展示（等待下一条字幕自动覆盖）。"""
        self._result_label.setText("[失败] " + message[:200])
        self._adjust_height()

    def show_status(self, text: str) -> None:
        """更新标题栏状态字样（如"已暂停"）。"""
        self._status_label.setText(text)

    def _archive_current(self) -> None:
        """把当前原文/译文压入历史区（仅在有完整译文时）。"""
        if not self._current_source or not self._current_text:
            return
        src = self._current_source.replace("\n", " ")
        res = self._current_text.replace("\n", " ")
        if len(src) > _HISTORY_MAX_CHARS:
            src = src[:_HISTORY_MAX_CHARS] + "…"
        if len(res) > _HISTORY_MAX_CHARS:
            res = res[:_HISTORY_MAX_CHARS] + "…"
        entry = QLabel(src + "\n" + res)
        entry.setProperty("historyEntry", True)
        entry.setWordWrap(True)
        # 新历史插到最上（离最新译文最近），超出条数删最旧
        self._history_lay.insertWidget(0, entry)
        while self._history_lay.count() - 1 > self._history_size:  # -1 去掉 stretch
            item = self._history_lay.takeAt(self._history_lay.count() - 2)
            if item is None:
                continue
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._current_source = ""
        self._current_text = ""

    def _adjust_height(self) -> None:
        """高度按内容自适应，上限为所在屏幕可用高度的 1/3。"""
        self.adjustSize()
        screen = self.screen() or QGuiApplication.primaryScreen()
        if screen is not None:
            max_h = screen.availableGeometry().height() // 3
            if self.height() > max_h:
                self.setFixedHeight(max_h)
            else:
                self.setMaximumHeight(max_h)
                self.setMinimumHeight(0)

    # ---------- 交互 ----------
    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and event.position().y() <= 32:
            self._drag_offset = (
                event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            )
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._drag_offset is not None:
            self.move(event.globalPosition().toPoint() - self._drag_offset)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        self._drag_offset = None
        super().mouseReleaseEvent(event)

    def _on_close(self) -> None:
        self.closed.emit()
        self.close()
        self.deleteLater()
