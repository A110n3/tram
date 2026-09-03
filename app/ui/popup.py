"""划词翻译悬浮窗。

无边框置顶小窗，首次显示时锚定在光标旁（多屏边界翻转），之后
状态切换（识别中 -> 翻译中 -> 完成/错误/缓存重显）保持原位不重
新定位；流式显示译文，失焦或超时后自动隐藏。译文区可选中复制，
内容过长时滚动显示。支持拖动和关闭按钮。

可选整体不透明度与鼠标穿透：穿透模式下浮窗不接收任何输入
（无法拖动/复制/关闭），点击直达底层窗口，标题栏随之隐藏，
改为纯展示字幕条；失焦隐藏退化为超时隐藏兜底。
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
# 穿透模式隐藏标题栏后的装饰高度：上边距(10) + 下边距(12) + 间距(6)
_CHROME_NO_HEADER = 28
# 最小窗口高度，避免空内容时坍缩
_MIN_HEIGHT = 60
# 最大高度下限：超小屏幕上 1/4 屏高可能只剩百来像素，
# 兜住避免窗口退化成一条缝（仍远低于任何正常屏幕的 1/4）
_MIN_MAX_HEIGHT = 180
# 等待首个 token 超过此时长后，提示"后端可能在加载模型"
_SLOW_HINT_MS = 8000
# 悬浮窗整体不透明度下限：低于此值文字难以阅读
_MIN_OPACITY = 0.3
# 穿透模式 + 失焦隐藏（auto_hide_ms=0）时的兜底隐藏时长：
# 穿透窗口永不失焦（也不应激活抢焦点），失焦隐藏永不触发，
# 不兜底的话浮窗将常驻直到下一次翻译
_CLICK_THROUGH_HIDE_MS = 10_000

# Token 批量合并：攒够一定时间的 token 一次性更新 UI，
# 避免每个 token 都触发 setText + heightForWidth 布局重算。
# 40ms 约合 25fps，人眼感觉不到延迟，但 UI 线程负载大幅降低
_TOKEN_BATCH_MS = 40


class TranslationPopup(QFrame):
    """无边框置顶悬浮窗，流式展示译文，长内容滚动显示。"""

    # 用户点 ✕：请求取消当前翻译（隐藏窗口由本类自行处理）
    close_requested = pyqtSignal()

    # 用户点 ⟳：请求取消当前翻译并用同一文本重新翻译
    retry_requested = pyqtSignal()

    def __init__(
        self,
        auto_hide_ms: int = 0,
        opacity: float = 1.0,
        click_through: bool = False,
        parent=None,
    ):
        super().__init__(
            parent,
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowStaysOnTopHint,
        )
        self.setObjectName("TramPopup")
        self._auto_hide_ms = auto_hide_ms
        self._click_through = False  # 实际状态以 set_click_through 为准
        self._drag_pos: QPoint | None = None
        self._loading_text = "翻译中…"  # 当前 loading 占位文案（OCR 用"识别中…"）
        self._load_started = 0.0  # show_loading 时刻，慢提示据此计算等待秒数
        self._current_text: str = ""  # Python 端译文缓存，避免每次从 QLabel 读回

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

        # Token 批量合并：攒 _TOKEN_BATCH_MS 的 token 一次性更新 UI
        self._token_buffer: list[str] = []
        self._batch_timer = QTimer(self)
        self._batch_timer.setSingleShot(True)
        self._batch_timer.timeout.connect(self._flush_token_buffer)

        self._build_ui()
        self._apply_style()
        self.set_opacity(opacity)
        self.set_click_through(click_through)

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

        self._drag_hint = QLabel("点击这里拖动浮窗")
        self._drag_hint.setStyleSheet("color: #5a6270; font-size: 11px;")
        self._drag_hint.setCursor(Qt.CursorShape.SizeAllCursor)

        self._close_btn = QPushButton("✕")  # ✕
        self._close_btn.setFixedSize(20, 20)
        self._close_btn.setFlat(True)
        self._close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._close_btn.setObjectName("TramClose")
        self._close_btn.clicked.connect(self._on_close_clicked)

        # 重试按钮：翻译相关状态（加载/流式/完成/缓存展示/失败）常驻显示，
        # 仅 OCR 识别阶段与无文本可重试的死路错误隐藏
        self._retry_btn = QPushButton("⟳")
        self._retry_btn.setFixedSize(20, 20)
        self._retry_btn.setFlat(True)
        self._retry_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._retry_btn.setObjectName("TramRetry")
        self._retry_btn.setToolTip("重新翻译")
        self._retry_btn.clicked.connect(self._on_retry_clicked)
        self._retry_btn.hide()

        header.addWidget(self._drag_hint)
        header.addStretch()
        header.addWidget(self._retry_btn)
        header.addWidget(self._close_btn)

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

        scroll = QScrollArea()
        scroll.setWidget(self._target_label)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        vbar = scroll.verticalScrollBar()
        assert vbar is not None  # QScrollArea 垂直滚动条总是存在
        self._vbar = vbar

        lay = QVBoxLayout(self)
        lay.setContentsMargins(POPUP_MARGIN, 10, POPUP_MARGIN, POPUP_MARGIN)
        lay.setSpacing(6)
        lay.addLayout(header)
        lay.addWidget(scroll)

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

    # ---------- 外观与交互模式 ----------
    def set_opacity(self, opacity: float) -> None:
        """设置整体不透明度，夹紧到 [_MIN_OPACITY, 1.0]。"""
        clamped = max(_MIN_OPACITY, min(1.0, opacity))
        self.setWindowOpacity(clamped)

    def set_click_through(self, enabled: bool) -> None:
        """切换鼠标穿透模式。

        启用后：窗口不接收任何输入（点击直达底层窗口），
        隐藏标题栏（拖动/关闭/重试按钮），失焦隐藏退化为超时兜底。
        """
        if self._click_through == enabled:
            return
        self._click_through = enabled
        # 显示/隐藏标题栏控件
        self._drag_hint.setVisible(not enabled)
        self._close_btn.setVisible(not enabled)
        # 重试按钮：穿透模式始终隐藏（无法点击），非穿透模式由业务逻辑控制
        if enabled:
            self._retry_btn.hide()
        # 文本选中：穿透模式下禁止（选中也无法复制，且会阻挡事件传递）
        if enabled:
            self._target_label.setTextInteractionFlags(
                Qt.TextInteractionFlag.NoTextInteraction
            )
        else:
            self._target_label.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse
            )
        # 窗口标志：WindowTransparentForInput
        was_visible = self.isVisible()
        flags = self.windowFlags()
        if enabled:
            flags |= Qt.WindowType.WindowTransparentForInput
        else:
            flags &= ~Qt.WindowType.WindowTransparentForInput
        # setWindowFlags 会隐藏窗口（Qt 需重建原生窗口），
        # 若原先可见则立即恢复显示并重算高度
        self.setWindowFlags(flags)
        if was_visible:
            self._adjust_height()
            self.show()

    def _restart_hide_timer(self) -> None:
        """重启自动隐藏计时器。

        穿透模式下若配置为失焦隐藏（auto_hide_ms=0），退化为
        _CLICK_THROUGH_HIDE_MS 兜底：穿透窗口永不失焦，失焦隐藏永不触发，
        不兜底的话浮窗将常驻直到下一次翻译。
        """
        if self._click_through and self._auto_hide_ms <= 0:
            self._timer.start(_CLICK_THROUGH_HIDE_MS)
        elif self._auto_hide_ms > 0:
            self._timer.start(self._auto_hide_ms)
        else:
            self._timer.stop()

    # ---------- 内容 ----------
    def show_loading(self, label: str = "翻译中…", can_retry: bool = False) -> None:
        """进入加载态。can_retry：是否允许重试（翻译阶段）。

        OCR 识别阶段不可重试（RapidOCR 进程内推理无法中断，
        重新识别直接再按热键框选即可），翻译阶段可中断重发。
        """
        self._flush_token_buffer()
        self._current_text = ""
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
        """流式追加译文（批量合并）。仅当用户未向上翻看时自动跟随到底部。

        token 先写入缓冲区，攒 _TOKEN_BATCH_MS 后一次性刷新到 UI，
        避免每个 token 都触发 setText + heightForWidth 布局重算。
        """
        self._stop_loading_hints()
        self._token_buffer.append(token)
        if not self._batch_timer.isActive():
            self._batch_timer.start(_TOKEN_BATCH_MS)
        # 流式期间重置自动隐藏倒计时：译文仍在产出，不应中途隐去
        # （慢后端从 show_loading 到首个 token 可远超隐藏时长）
        self._restart_hide_timer()

    def _flush_token_buffer(self) -> None:
        """把缓冲区的 token 合并后一次性更新 UI。"""
        if not self._token_buffer:
            return
        bar = self._vbar
        stick_to_bottom = bar.value() >= bar.maximum() - 4

        new_tokens = "".join(self._token_buffer)
        self._token_buffer.clear()

        # 首 token：替换掉 loading 占位文案
        if not self._current_text:
            self._current_text = new_tokens
        else:
            self._current_text += new_tokens

        self._target_label.setText(self._current_text)
        self._adjust_height()
        if stick_to_bottom:
            # 延迟到布局完成后滚动，确保滚动条 range 已按新内容更新
            QTimer.singleShot(0, self._scroll_to_bottom)

    def set_translation(self, text: str) -> None:
        self._stop_loading_hints()
        self._flush_token_buffer()
        self._current_text = text
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
        self._flush_token_buffer()
        self._current_text = ""
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
        self._flush_token_buffer()
        self._current_text = ""
        self._target_label.setStyleSheet("")  # 清除错误样式
        self._target_label.setText("正在捕获…")
        self._scroll_to_top()
        self._adjust_height()
        self._anchor_at_cursor_if_needed()
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
        self._flush_token_buffer()
        self._current_text = text
        self._retry_btn.show()
        self._target_label.setStyleSheet("")  # 清除错误样式
        self._target_label.setText(text)
        self._scroll_to_top()
        self._show()

    def fade_out(self, text: str = "未检测到选中文本") -> None:
        """短暂显示提示文案后自动隐藏（捕获失败/无识别结果）。"""
        self._stop_loading_hints()
        self._flush_token_buffer()
        self._current_text = ""
        self._retry_btn.hide()
        self._target_label.setText(text)
        self._target_label.setStyleSheet("color: #9aa0ac;")
        self._scroll_to_top()
        self._adjust_height()
        self._anchor_at_cursor_if_needed()
        self.show()
        self.raise_()
        QTimer.singleShot(800, self.hide)

    # ---------- 显示/隐藏 ----------
    def _show(self) -> None:
        self._adjust_height()
        self._anchor_at_cursor_if_needed()
        self.show()
        self.raise_()
        if not self._click_through:
            # 穿透模式不激活抢焦点：用户正与底层窗口交互（游戏/视频），
            # 抢焦点会打断操作，且穿透窗口失焦隐藏永不触发
            self.activateWindow()
            self.setFocus()
        self._restart_hide_timer()

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
        # + header(close btn 20)；穿透模式隐藏标题栏，chrome 更矮
        chrome = _CHROME_NO_HEADER if self._click_through else _CHROME_HEIGHT
        total = tgt_h + chrome
        # 上限内随内容增长；触顶后 QScrollArea 出现滚动条展示溢出
        self.setFixedHeight(min(max(total, _MIN_HEIGHT), self._max_window_height()))
        if self.isVisible():
            # 高度增长（加载态 -> 流式译文）可能把已锚定的窗口推出屏幕，
            # 仅收回可视区域，不改变锚点位置
            self._keep_on_screen()

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

    def _anchor_at_cursor_if_needed(self) -> None:
        """仅在窗口尚未显示时锚定到光标旁。

        同一弹窗实例要经历多次状态切换（识别中 -> 翻译中 ->
        完成/错误/缓存重显），若每次都重新跟随光标定位，鼠标移动后
        窗口会"瞬移"到新位置，看起来像关闭重开（OCR 识别耗时数秒，
        期间鼠标几乎必然移动）。已显示的窗口保持原位，用户可通过
        拖动自行调整位置。
        """
        if not self.isVisible():
            self._position_at_cursor()

    def _keep_on_screen(self) -> None:
        """把已显示的窗口收回所在屏幕的可视区域（不改变锚点）。"""
        screen = self.screen()
        if screen is None:
            return
        geo = screen.availableGeometry()
        x = max(geo.left(), min(self.x(), geo.right() - self.width()))
        y = max(geo.top(), min(self.y(), geo.bottom() - self.height()))
        if x != self.x() or y != self.y():
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
