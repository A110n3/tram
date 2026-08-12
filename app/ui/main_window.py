"""Tram 划词翻译 — 托盘常驻，热键触发取词翻译。"""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QFont, QIcon, QPainter, QPixmap
from PyQt6.QtWidgets import (
    QApplication,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

from ..config import save_config
from .glossary_dialog import GlossaryDialog
from .settings_dialog import SettingsDialog
from .selection_translator import SelectionTranslator


class MainWindow(QMainWindow):
    def __init__(self, config: dict):
        super().__init__()
        self._config = config
        self._quitting = False

        self.setWindowTitle("Tram 划词翻译")
        self.resize(320, 140)
        self._build_ui()

        # 划词翻译服务
        self._selection_translator = SelectionTranslator(self._config)
        self._selection_translator.hotkey_status.connect(self._on_hotkey_status)

        # 托盘
        self._create_tray()
        self._apply_selection_config()

        # 启动后隐藏主窗口，仅托盘运行
        self.hide()

    # ---------- UI ----------
    def _build_ui(self) -> None:
        tools_menu = self.menuBar().addMenu("工具")
        tools_menu.addAction("设置…").triggered.connect(self.open_settings)
        tools_menu.addAction("术语表…").triggered.connect(self.open_glossary)

        central = QWidget()
        lay = QVBoxLayout(central)
        lay.setContentsMargins(24, 16, 24, 16)

        title = QLabel("Tram 划词翻译")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setStyleSheet("font-size: 20px; font-weight: bold; color: #4a9eff;")
        hint = QLabel("已在系统托盘运行\n右键托盘图标管理划词或打开设置")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hint.setStyleSheet("color: #788090; font-size: 13px;")

        lay.addStretch()
        lay.addWidget(title)
        lay.addSpacing(4)
        lay.addWidget(hint)
        lay.addStretch()
        self.setCentralWidget(central)

    # ---------- 托盘 ----------
    def _create_tray(self) -> None:
        self._tray_icon = QSystemTrayIcon(self)
        pixmap = self._make_tray_icon()
        self._tray_icon.setIcon(QIcon(pixmap))
        self._tray_icon.setToolTip("Tram 划词翻译")

        menu = QMenu()
        show_action = menu.addAction("显示主窗口")
        show_action.triggered.connect(self._show_from_tray)
        menu.addSeparator()
        self._selection_action = menu.addAction("划词翻译")
        self._selection_action.setCheckable(True)
        self._selection_action.triggered.connect(self._toggle_selection)
        menu.addSeparator()
        menu.addAction("设置…").triggered.connect(self.open_settings)
        menu.addAction("术语表…").triggered.connect(self.open_glossary)
        menu.addSeparator()
        menu.addAction("退出").triggered.connect(self.quit_app)

        self._tray_icon.setContextMenu(menu)
        self._tray_icon.show()

    def _make_tray_icon(self) -> QPixmap:
        pixmap = QPixmap(32, 32)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setBrush(QColor("#4a9eff"))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRoundedRect(2, 2, 28, 28, 6, 6)
        painter.setPen(QColor("white"))
        font = QFont("Microsoft YaHei", 14, QFont.Weight.Bold)
        painter.setFont(font)
        painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, "译")
        painter.end()
        return pixmap

    def _show_from_tray(self) -> None:
        self.show()
        self.raise_()
        self.activateWindow()

    def _toggle_selection(self) -> None:
        enabled = self._selection_action.isChecked()
        self._config["selection"]["enabled"] = enabled
        save_config(self._config)
        if enabled:
            self._selection_translator.start()
        else:
            self._selection_translator.stop()
        self._tray_icon.setToolTip(
            f"Tram - 划词: {'开' if enabled else '关'}"
        )

    def _apply_selection_config(self) -> None:
        sel = self._config.get("selection", {})
        enabled = sel.get("enabled", False)
        self._selection_action.setChecked(enabled)
        if enabled:
            self._selection_translator.start()
            self._tray_icon.showMessage(
                "Tram 划词",
                f"划词翻译已开启，热键: {sel.get('hotkey', 'Ctrl+Shift+T')}",
                QSystemTrayIcon.MessageIcon.Information,
                4000,
            )
        else:
            self._tray_icon.showMessage(
                "Tram 划词",
                "点击托盘图标开启划词翻译",
                QSystemTrayIcon.MessageIcon.Information,
                4000,
            )

    def _on_hotkey_status(self, ok: bool, message: str) -> None:
        if ok:
            self._tray_icon.showMessage(
                "Tram 划词", message,
                QSystemTrayIcon.MessageIcon.Information, 3000,
            )
        else:
            self._tray_icon.showMessage(
                "Tram 划词", f"热键注册失败: {message}",
                QSystemTrayIcon.MessageIcon.Warning, 5000,
            )

    # ---------- 菜单 ----------
    def open_settings(self) -> None:
        dlg = SettingsDialog(self._config, self)
        if dlg.exec():
            save_config(self._config)
            self._selection_translator.rebuild_backend()
            self._selection_action.setChecked(
                self._config.get("selection", {}).get("enabled", False)
            )

    def open_glossary(self) -> None:
        from ..core import glossary as gs

        dlg = GlossaryDialog(self)
        if dlg.exec():
            self._config["glossary"] = gs.load_glossary()

    # ---------- 退出 ----------
    def quit_app(self) -> None:
        self._quitting = True
        self._selection_translator.shutdown()
        QApplication.instance().quit()

    def closeEvent(self, event) -> None:
        if self._quitting:
            super().closeEvent(event)
            return
        if self._config.get("selection", {}).get("enabled", False):
            self.hide()
            self._tray_icon.showMessage(
                "Tram", "已最小化到系统托盘，划词翻译仍在运行。",
                QSystemTrayIcon.MessageIcon.Information, 2000,
            )
            event.ignore()
        else:
            self.quit_app()
