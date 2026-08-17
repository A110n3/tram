"""全屏选区覆盖层（OCR 框选）。

显示冻结的屏幕截图并压暗，拖拽框选目标区域：框内还原原始亮度，
边框高亮。鼠标释放后发出 region_selected(QRect)；框选小于 8×8、
按 ESC 或右键视为取消，发出 cancelled。

v1 只覆盖主屏（多屏留 v2）。坐标为设备无关像素（逻辑像素），
裁剪时由调用方乘 devicePixelRatio 换算截图真实像素。
"""

from __future__ import annotations

import contextlib

from PyQt6.QtCore import QPoint, QRect, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QPainter, QPen, QPixmap, QRegion
from PyQt6.QtWidgets import QWidget

# 框选小于此尺寸（宽或高）视为误触/取消
_MIN_REGION_SIZE = 8
_DIM_COLOR = QColor(0, 0, 0, 120)
_BORDER_COLOR = QColor("#4a9eff")


class RegionOverlay(QWidget):
    """全屏置顶选区覆盖层，背景为冻结截图 + 半透明遮罩。"""

    region_selected = pyqtSignal(QRect)
    cancelled = pyqtSignal()

    def __init__(self, screenshot: QPixmap, parent=None):
        super().__init__(
            parent,
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool,
        )
        self._screenshot = screenshot
        self._origin: QPoint | None = None  # 拖拽起点（逻辑坐标）
        self._selection: QRect | None = None
        self._done = False  # 已结束（发出信号并关闭），防重复触发
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setMouseTracking(True)

        # 覆盖整个主屏；截图必须与该几何一致（调用方用同一 screen 截取）
        screen = self.screen()
        if screen is None:  # 无屏极端情况，无法弹覆盖层
            raise RuntimeError("无法获取主屏几何信息")
        geo = screen.geometry()
        self.setGeometry(geo)

    # ---------- 绘制 ----------
    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        geo = self.geometry()
        # 画面冻结：绘制热键按下前截好的屏幕截图
        painter.drawPixmap(
            0, 0, geo.width(), geo.height(), self._screenshot
        )
        sel = self._selection
        if sel is None or sel.width() <= 0 or sel.height() <= 0:
            # 未开始拖拽：全屏压暗
            painter.fillRect(0, 0, geo.width(), geo.height(), _DIM_COLOR)
        else:
            # 拖拽中：仅框外压暗，框内还原原始亮度
            painter.save()
            painter.setClipRegion(self._frame_region(geo, sel))
            painter.fillRect(0, 0, geo.width(), geo.height(), _DIM_COLOR)
            painter.restore()
            # 边框高亮
            pen = QPen(_BORDER_COLOR, 2)
            painter.setPen(pen)
            painter.drawRect(sel.normalized())
        painter.end()

    @staticmethod
    def _frame_region(geo: QRect, sel: QRect) -> QRegion:
        """框外区域 = 全屏减去选区（用于压暗）。"""
        full = QRegion(0, 0, geo.width(), geo.height())
        return full - QRegion(sel.normalized())

    # ---------- 交互 ----------
    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            pos = event.position().toPoint()
            self._origin = pos
            self._selection = QRect(pos, pos)
            self.update()
        elif event.button() == Qt.MouseButton.RightButton:
            self._finish(cancel=True)
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._origin is not None:
            pos = event.position().toPoint()
            self._selection = QRect(self._origin, pos).normalized()
            self.update()
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._origin is not None:
            sel = QRect(self._origin, event.position().toPoint()).normalized()
            self._origin = None
            if sel.width() < _MIN_REGION_SIZE or sel.height() < _MIN_REGION_SIZE:
                self._finish(cancel=True)
            else:
                self._selection = sel
                self._finish(cancel=False)
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self._finish(cancel=True)
        super().keyPressEvent(event)

    # ---------- 结束 ----------
    def _finish(self, cancel: bool) -> None:
        """结束选区：发信号并关闭窗口。_done 标志防止重复触发。"""
        if self._done:
            return
        self._done = True
        sel = self._selection.normalized() if self._selection else QRect()
        # 先断开不再相关的一个信号，再 emit + 关闭，
        # 避免关闭过程中的事件再次进入本方法造成双发
        if cancel:
            with contextlib.suppress(TypeError):
                self.region_selected.disconnect()
            self.cancelled.emit()
        else:
            with contextlib.suppress(TypeError):
                self.cancelled.disconnect()
            self.region_selected.emit(sel)
        self.close()
        self.deleteLater()
