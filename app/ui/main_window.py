"""Tram 划词翻译 — 托盘常驻，热键触发取词翻译。"""

from __future__ import annotations

import contextlib
from collections.abc import Callable

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QAction, QActionGroup, QColor, QFont, QIcon, QPainter, QPixmap
from PyQt6.QtWidgets import (
    QApplication,
    QLabel,
    QMainWindow,
    QMenu,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

from ..config import save_config
from ..core.prompts import TARGET_LANGS
from .glossary_dialog import GlossaryDialog
from .selection_translator import SelectionTranslator
from .settings_dialog import SettingsDialog
from .worker import TestConnectionWorker
from .worker_util import track_worker


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

        # 切换目标语言后的连接测试线程
        self._lang_test_worker: TestConnectionWorker | None = None
        self._lang_test_lang: str = ""

        # 托盘
        self._create_tray()
        self._apply_selection_config()

        # 启动后隐藏主窗口，仅托盘运行
        self.hide()

    # ---------- UI ----------
    @staticmethod
    def _act(action: QAction | None, slot: Callable[[], None]) -> QAction:
        """QMenu.addAction 的类型签名为 QAction | None，实际总是成功。

        统一断言收窄类型并连接 triggered 信号，避免到处写 assert。
        """
        assert action is not None
        action.triggered.connect(slot)
        return action

    def _build_ui(self) -> None:
        menubar = self.menuBar()
        assert menubar is not None  # QMainWindow.menuBar() 总是存在
        tools_menu = menubar.addMenu("工具")
        assert tools_menu is not None
        self._act(tools_menu.addAction("设置…"), self.open_settings)
        self._act(tools_menu.addAction("术语表…"), self.open_glossary)

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
        self._act(menu.addAction("显示主窗口"), self._show_from_tray)
        menu.addSeparator()
        self._selection_action = self._act(menu.addAction("划词翻译"), self._toggle_selection)
        self._selection_action.setCheckable(True)

        # 目标语言快捷子菜单
        lang_menu = menu.addMenu("目标语言")
        assert lang_menu is not None
        self._lang_group = QActionGroup(self)
        self._lang_group.setExclusive(True)
        current_lang = self._config.get("translation", {}).get(
            "target_lang", "中文（简体）"
        )
        for lang in TARGET_LANGS:
            act = self._lang_group.addAction(lang)
            assert act is not None  # QActionGroup.addAction 总是返回 QAction
            act.setCheckable(True)
            act.setChecked(lang == current_lang)
            lang_menu.addAction(act)
        self._lang_group.triggered.connect(self._on_target_lang_changed)

        menu.addSeparator()
        self._act(menu.addAction("设置…"), self.open_settings)
        self._act(menu.addAction("术语表…"), self.open_glossary)
        menu.addSeparator()
        self._act(menu.addAction("退出"), self.quit_app)

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

    def _on_target_lang_changed(self, action: QAction) -> None:
        """托盘菜单快捷切换目标语言。

        即时保存配置，随后后台测试模型连接，测试通过后才弹出
        切换成功通知；失败则弹出警告，避免用户以为已生效。
        """
        lang = action.text()
        self._config.setdefault("translation", {})["target_lang"] = lang
        save_config(self._config)
        self._start_lang_test(lang)

    def _start_lang_test(self, lang: str) -> None:
        """后台测试模型连接，结果由 _on_lang_test_ok/_err 通知。"""
        self._drop_lang_test_worker()
        self._lang_test_lang = lang
        b = self._config.get("backend", {})
        w = TestConnectionWorker(
            b.get("base_url", ""),
            b.get("api_key", "ollama"),
            b.get("model", ""),
            use_system_role=bool(b.get("use_system_role", True)),
            parent=self,
        )
        self._lang_test_worker = w
        w.ok.connect(self._on_lang_test_ok)
        w.err.connect(self._on_lang_test_err)
        # 线程结束：先清 Python 引用、再删 C++ 对象，避免残留僵尸包装器
        track_worker(self, "_lang_test_worker", w)
        w.start()

    def _drop_lang_test_worker(self) -> None:
        """作废上一次未完成的测试：断开结果信号，线程自行结束并清理。

        快速连续切换语言时，只认最新一次测试的结果。
        """
        w = self._lang_test_worker
        if w is None:
            return
        with contextlib.suppress(TypeError, RuntimeError):
            w.ok.disconnect(self._on_lang_test_ok)
        with contextlib.suppress(TypeError, RuntimeError):
            w.err.disconnect(self._on_lang_test_err)
        self._lang_test_worker = None

    def _on_lang_test_ok(self, _reply: str) -> None:
        self._lang_test_worker = None
        self._tray_icon.showMessage(
            "Tram 划词", f"目标语言已切换为 {self._lang_test_lang}",
            QSystemTrayIcon.MessageIcon.Information, 2000,
        )

    def _on_lang_test_err(self, message: str) -> None:
        self._lang_test_worker = None
        self._tray_icon.showMessage(
            "Tram 划词",
            f"目标语言已切换为 {self._lang_test_lang}，"
            f"但模型连接测试失败：\n{message.strip()[:120]}",
            QSystemTrayIcon.MessageIcon.Warning, 5000,
        )

    def _apply_selection_config(self) -> None:
        sel = self._config.get("selection", {})
        enabled = sel.get("enabled", False)
        self._selection_action.setChecked(enabled)
        if enabled:
            self._selection_translator.start()
            self._tray_icon.showMessage(
                "Tram 划词",
                f"划词翻译已开启，热键: {sel.get('hotkey', 'Ctrl+F4')}",
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
            # 重建后端（切换模型时生效），并重新注册热键
            self._selection_translator.rebuild_backend()
            # 同步托盘菜单勾选状态与 tooltip
            self._apply_selection_config()
            # 同步托盘目标语言单选
            self._sync_target_lang_action()

    def _sync_target_lang_action(self) -> None:
        """设置保存后同步托盘菜单的目标语言选中状态。"""
        current = self._config.get("translation", {}).get(
            "target_lang", "中文（简体）"
        )
        for action in self._lang_group.actions():
            action.setChecked(action.text() == current)

    def open_glossary(self) -> None:
        from ..core import glossary as gs

        dlg = GlossaryDialog(self)
        if dlg.exec():
            self._config["glossary"] = gs.load_glossary()

    # ---------- 退出 ----------
    def quit_app(self) -> None:
        self._quitting = True
        # 等待进行中的连接测试退出，避免 QThread 析构时仍在运行
        w = self._lang_test_worker
        self._lang_test_worker = None
        if w is not None:
            try:
                if w.isRunning():
                    w.wait(2000)
            except RuntimeError:
                pass  # 僵尸包装器（C++ 对象已删除），无需处理
        self._selection_translator.shutdown()
        app = QApplication.instance()
        if app is not None:
            app.quit()

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
