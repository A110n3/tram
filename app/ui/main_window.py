"""主窗口：双栏对照翻译，流式输出，后台线程执行，可随时停止。"""

from __future__ import annotations

import time

from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QApplication,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from ..config import save_config
from ..core.backend import OpenAIBackend
from ..core.translator import Translator
from .glossary_dialog import GlossaryDialog
from .settings_dialog import SettingsDialog

TARGET_LANGS = [
    "中文（简体）",
    "中文（繁體）",
    "English",
    "日本語",
    "한국어",
    "Français",
    "Deutsch",
    "Español",
    "Русский",
]


class _StopRequested(Exception):
    """用户点击停止时抛出，用于中断流式翻译。"""


class TranslateWorker(QThread):
    token = pyqtSignal(str)
    chunk = pyqtSignal(int, int)
    succeeded = pyqtSignal(str)
    failed = pyqtSignal(str)

    def __init__(self, translator: Translator, text: str, parent=None):
        super().__init__(parent)
        self._translator = translator
        self._text = text
        self._stop_flag = False

    def request_stop(self) -> None:
        self._stop_flag = True

    def run(self) -> None:
        def on_token(t: str) -> None:
            if self._stop_flag:
                raise _StopRequested()
            self.token.emit(t)

        try:
            result = self._translator.translate(
                self._text,
                on_token=on_token,
                on_chunk=lambda i, n: self.chunk.emit(i, n),
            )
            if not self._stop_flag:
                self.succeeded.emit(result)
        except _StopRequested:
            pass  # 用户主动停止，静默结束
        except Exception as e:  # 网络错误、后端错误等
            self.failed.emit(str(e))


class MainWindow(QMainWindow):
    def __init__(self, config: dict):
        super().__init__()
        self._config = config
        self._worker: TranslateWorker | None = None
        self._suppress_failure = False  # 主动中断翻译时不弹错误框
        self._backend = self._make_backend()

        self.setWindowTitle("Tram 离线翻译")
        self.resize(1000, 640)
        self._build_ui()
        self._apply_config_to_ui()
        self._update_status()

    # ---------- 后端 ----------
    def _make_backend(self) -> OpenAIBackend:
        b = self._config.get("backend", {})
        return OpenAIBackend(
            base_url=b.get("base_url", ""),
            api_key=b.get("api_key", "ollama"),
            model=b.get("model", ""),
            timeout=int(b.get("timeout", 180)),
        )

    def _rebuild_backend(self) -> None:
        try:
            self._backend.close()
        except Exception:
            pass
        self._backend = self._make_backend()

    def _stop_active_translation(self) -> None:
        """停止并等待当前翻译会话结束。

        切换模型/后端前调用。本地推理服务切换模型时需重新装载，
        期间不产出 token，request_stop 依赖的 on_token 检查无法触发；
        故先等待软停止，超时则主动关闭后端连接以打断阻塞的流式请求。
        """
        worker = self._worker
        if not worker or not worker.isRunning():
            return
        self._suppress_failure = True  # 主动中断引发的失败不应弹错误框
        worker.request_stop()
        self.status.showMessage("正在停止当前翻译，以便切换模型…")
        worker.wait(2000)
        if worker.isRunning():
            # 模型装载中无 token 产出，软停止未生效，关闭连接强行打断
            try:
                self._backend.close()
            except Exception:
                pass
            worker.wait(3000)

    # ---------- UI ----------
    def _build_ui(self) -> None:
        # 菜单
        file_menu = self.menuBar().addMenu("文件")
        quit_action = file_menu.addAction("退出")
        quit_action.triggered.connect(self.close)

        tools_menu = self.menuBar().addMenu("工具")
        settings_action = tools_menu.addAction("设置…")
        settings_action.triggered.connect(self.open_settings)
        glossary_action = tools_menu.addAction("术语表…")
        glossary_action.triggered.connect(self.open_glossary)

        # 工具栏区
        toolbar = QWidget()
        bar = QHBoxLayout(toolbar)
        bar.setContentsMargins(8, 8, 8, 4)

        self.translate_btn = QPushButton("翻译")
        self.translate_btn.clicked.connect(self.start_translate)
        self.stop_btn = QPushButton("停止")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self.stop_translate)
        self.clear_btn = QPushButton("清空")
        self.clear_btn.clicked.connect(self.clear_all)

        self.lang_combo = QComboBox()
        self.lang_combo.setEditable(True)
        self.lang_combo.addItems(TARGET_LANGS)

        bar.addWidget(self.translate_btn)
        bar.addWidget(self.stop_btn)
        bar.addWidget(self.clear_btn)
        bar.addSpacing(16)
        bar.addWidget(QLabel("译为目标语言："))
        bar.addWidget(self.lang_combo)
        bar.addStretch(1)

        # 双栏文本区
        self.source_edit = QPlainTextEdit()
        self.source_edit.setPlaceholderText(
            "在此粘贴或输入要翻译的文本…（支持多段落长文本）"
        )
        self.target_edit = QPlainTextEdit()
        self.target_edit.setReadOnly(True)
        self.target_edit.setPlaceholderText("译文将在这里流式显示…")

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self._pane("原文", self.source_edit))
        splitter.addWidget(self._pane("译文", self.target_edit))
        splitter.setSizes([500, 500])

        central = QWidget()
        root = QVBoxLayout(central)
        root.addWidget(toolbar)
        root.addWidget(splitter, 1)
        self.setCentralWidget(central)

        # 状态栏
        self.status = self.statusBar()

    def _pane(self, title: str, edit: QPlainTextEdit) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(QLabel(title))
        lay.addWidget(edit, 1)
        return w

    # ---------- 配置 ----------
    def _apply_config_to_ui(self) -> None:
        t = self._config.get("translation", {})
        lang = t.get("target_lang", "中文（简体）")
        self.lang_combo.setCurrentText(lang)

    def _update_status(self) -> None:
        b = self._config.get("backend", {})
        self.status.showMessage(
            f"后端：{b.get('base_url', '未配置')}   模型：{b.get('model', '未配置')}"
        )

    # ---------- 动作 ----------
    def start_translate(self) -> None:
        text = self.source_edit.toPlainText().strip()
        if not text:
            QMessageBox.information(self, "提示", "请先输入要翻译的文本。")
            return
        if self._worker and self._worker.isRunning():
            return

        self._config["translation"]["target_lang"] = self.lang_combo.currentText()
        save_config(self._config)

        self.target_edit.clear()
        self.translate_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.status.showMessage("翻译中…")

        translator = Translator(self._backend, self._config)
        self._worker = TranslateWorker(translator, text)
        self._worker.token.connect(
            lambda t: self.target_edit.insertPlainText(t)
        )
        self._worker.succeeded.connect(self._on_success)
        self._worker.failed.connect(self._on_failed)
        self._worker.finished.connect(self._on_worker_finished)
        self._worker.start()

    def stop_translate(self) -> None:
        if self._worker and self._worker.isRunning():
            self._worker.request_stop()
            self.status.showMessage("正在停止…")

    def clear_all(self) -> None:
        self.source_edit.clear()
        self.target_edit.clear()

    def _on_success(self, _result: str) -> None:
        self.status.showMessage("翻译完成")

    def _on_failed(self, message: str) -> None:
        self.status.showMessage("翻译失败")
        if self._suppress_failure:
            return  # 切换模型时主动中断引发的失败，静默
        QMessageBox.critical(
            self, "翻译失败",
            f"{message}\n\n请检查设置中的后端地址、模型名称是否正确，"
            "以及本地推理服务是否已启动。",
        )

    def _on_worker_finished(self) -> None:
        self.translate_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self._suppress_failure = False
        if self._worker:
            self._worker = None

    # ---------- 菜单 ----------
    def open_settings(self) -> None:
        dlg = SettingsDialog(self._config, self)
        if dlg.exec():
            # 切换模型前先停止当前翻译会话：本地模型切换需重新装载，
            # 若不停止，旧请求会 hang 在装载阶段且复用旧 backend 会出错。
            self._stop_active_translation()
            save_config(self._config)
            self._rebuild_backend()
            self._update_status()
            self.status.showMessage(
                f"已切换到模型：{self._config['backend'].get('model', '未配置')}"
            )

    def open_glossary(self) -> None:
        # 打开前把当前术语表读入配置，供翻译编排使用
        from ..core import glossary as gs

        dlg = GlossaryDialog(self)
        if dlg.exec():
            self._config["glossary"] = gs.load_glossary()
            self.status.showMessage(
                f"术语表已保存，共 {len(self._config['glossary'])} 条。"
            )

    def closeEvent(self, event) -> None:
        if self._worker and self._worker.isRunning():
            self._worker.request_stop()
            self._worker.wait(3000)
        try:
            self._backend.close()
        except Exception:
            pass
        super().closeEvent(event)
