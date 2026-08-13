"""设置对话框：后端连接配置 + 翻译参数 + 连接测试。"""

from __future__ import annotations

import logging

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QKeySequence
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QKeySequenceEdit,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ..core.hotkey import HotkeyError, parse_hotkey, test_hotkey_available
from ..core.prompts import SOURCE_LANGS, TARGET_LANGS
from .worker import TestConnectionWorker

logger = logging.getLogger(__name__)

# 常用本地后端预设
PRESETS = {
    "Ollama": "http://localhost:11434/v1",
    "LM Studio": "http://localhost:1234/v1",
    "vLLM": "http://localhost:8000/v1",
}


class SettingsDialog(QDialog):
    def __init__(self, config: dict, parent=None):
        super().__init__(parent)
        self._config = config
        self.setWindowTitle("设置")
        self.setMinimumWidth(460)
        self._test_worker: TestConnectionWorker | None = None
        self._build_ui()
        self._load_values()

    # ---------- UI ----------
    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        backend_box = QWidget()
        bf = QFormLayout(backend_box)
        bf.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self.preset_combo = QComboBox()
        self.preset_combo.addItem("自定义", "")
        for name, url in PRESETS.items():
            self.preset_combo.addItem(name, url)
        self.preset_combo.currentIndexChanged.connect(self._on_preset)

        self.base_url_edit = QLineEdit()
        self.api_key_edit = QLineEdit()
        self.api_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.model_edit = QLineEdit()
        self.model_edit.setPlaceholderText("例如 qwen2.5:7b / llama3.1:8b")

        self.use_system_role_cb = QCheckBox("使用 system 消息")
        self.use_system_role_cb.setToolTip(
            "翻译指令以 system 角色消息发送。部分后端不支持 system 消息"
            "（请求会返回 5xx 错误），遇到翻译报错 500/502 时可取消勾选，"
            "改为将指令并入用户消息发送。"
        )

        bf.addRow("后端预设", self.preset_combo)
        bf.addRow("Base URL", self.base_url_edit)
        bf.addRow("API Key", self.api_key_edit)
        bf.addRow("模型名称", self.model_edit)
        bf.addRow("", self.use_system_role_cb)

        test_row = QHBoxLayout()
        self.test_btn = QPushButton("测试连接")
        self.test_btn.clicked.connect(self._on_test)
        self.test_result = QLabel("")
        self.test_result.setWordWrap(True)
        test_row.addWidget(self.test_btn)
        test_row.addWidget(self.test_result, 1)
        bf.addRow("", test_row)

        layout.addWidget(backend_box)

        trans_box = QWidget()
        tf = QFormLayout(trans_box)
        tf.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self.temperature_spin = QDoubleSpinBox()
        self.temperature_spin.setRange(0.0, 2.0)
        self.temperature_spin.setSingleStep(0.1)

        self.max_tokens_spin = QSpinBox()
        self.max_tokens_spin.setRange(128, 32768)
        self.max_tokens_spin.setSingleStep(128)

        self.chunk_spin = QSpinBox()
        self.chunk_spin.setRange(256, 8000)
        self.chunk_spin.setSingleStep(256)
        self.chunk_spin.setSuffix(" 字符")

        self.style_combo = QComboBox()
        self.style_combo.addItems(["忠实原文", "自然流畅", "简洁精炼"])

        self.source_lang_combo = QComboBox()
        self.source_lang_combo.addItems(SOURCE_LANGS)
        self.source_lang_combo.setToolTip(
            "选择源语言可辅助翻译。默认自动识别，由模型自行判断。"
        )

        self.target_lang_combo = QComboBox()
        self.target_lang_combo.addItems(TARGET_LANGS)

        tf.addRow("源语言", self.source_lang_combo)
        tf.addRow("目标语言", self.target_lang_combo)
        tf.addRow("温度", self.temperature_spin)
        tf.addRow("最大输出 tokens", self.max_tokens_spin)
        tf.addRow("分块长度", self.chunk_spin)
        tf.addRow("翻译风格", self.style_combo)

        layout.addWidget(trans_box)

        # 划词翻译分组
        sel_gb = QGroupBox("划词翻译")
        sl = QFormLayout(sel_gb)
        sl.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self.selection_enabled_cb = QCheckBox("启用划词翻译")
        self.selection_hotkey_edit = QKeySequenceEdit()
        self.selection_hotkey_edit.keySequenceChanged.connect(
            self._validate_hotkey
        )
        self._hotkey_validation = QLabel("")
        self._hotkey_validation.setStyleSheet("color: #888; font-size: 11px;")
        self.selection_min_chars_spin = QSpinBox()
        self.selection_min_chars_spin.setRange(1, 50)
        self.selection_min_chars_spin.setSingleStep(1)

        sl.addRow("", self.selection_enabled_cb)
        sl.addRow("全局热键", self.selection_hotkey_edit)
        sl.addRow("", self._hotkey_validation)
        sl.addRow("最小字符数", self.selection_min_chars_spin)

        layout.addWidget(sel_gb)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._on_save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    # ---------- 数据 ----------
    def _load_values(self) -> None:
        b = self._config.get("backend", {})
        t = self._config.get("translation", {})

        url = b.get("base_url", "")
        idx = self.preset_combo.findData(url)
        if idx >= 0:
            self.preset_combo.setCurrentIndex(idx)
        self.base_url_edit.setText(url)
        self.api_key_edit.setText(b.get("api_key", ""))
        self.model_edit.setText(b.get("model", ""))
        self.use_system_role_cb.setChecked(bool(b.get("use_system_role", True)))
        self.temperature_spin.setValue(float(b.get("temperature", 0.2)))
        self.max_tokens_spin.setValue(int(b.get("max_tokens", 2048)))
        self.chunk_spin.setValue(int(t.get("chunk_chars", 2000)))
        source_lang = t.get("source_lang", "自动识别")
        if self.source_lang_combo.findText(source_lang) >= 0:
            self.source_lang_combo.setCurrentText(source_lang)
        target_lang = t.get("target_lang", "中文（简体）")
        if self.target_lang_combo.findText(target_lang) >= 0:
            self.target_lang_combo.setCurrentText(target_lang)
        style = t.get("style", "忠实原文")
        if self.style_combo.findText(style) >= 0:
            self.style_combo.setCurrentText(style)

        sel = self._config.get("selection", {})
        self.selection_enabled_cb.setChecked(sel.get("enabled", False))
        self.selection_hotkey_edit.setKeySequence(
            QKeySequence.fromString(
                sel.get("hotkey", "Ctrl+F4"),
                QKeySequence.SequenceFormat.PortableText,
            )
        )
        self.selection_min_chars_spin.setValue(int(sel.get("min_chars", 2)))
        # 初始校验当前热键
        self._validate_hotkey(None)

    def _on_preset(self) -> None:
        url = self.preset_combo.currentData()
        if url:
            self.base_url_edit.setText(url)

    def _on_save(self) -> None:
        if not self.model_edit.text().strip():
            QMessageBox.warning(self, "提示", "请填写模型名称。")
            return

        hotkey_spec = self.selection_hotkey_edit.keySequence().toString(
            QKeySequence.SequenceFormat.PortableText
        ) or "Ctrl+F4"

        # 格式校验：先看 parse_hotkey 能不能解析
        try:
            parse_hotkey(hotkey_spec)
        except HotkeyError as e:
            self._hotkey_validation.setText(f"✗ {e}")
            self._hotkey_validation.setStyleSheet(
                "color: #c0392b; font-size: 11px;"
            )
            self.selection_hotkey_edit.setFocus()
            return

        # 只校验与当前已注册热键不同的新键；相同则跳过（必然可用）
        current_hotkey = self._config.get("selection", {}).get("hotkey", "")
        if hotkey_spec != current_hotkey:
            ok, err = test_hotkey_available(hotkey_spec)
            if not ok:
                self._hotkey_validation.setText(f"✗ {err}")
                self._hotkey_validation.setStyleSheet(
                    "color: #c0392b; font-size: 11px;"
                )
                self.selection_hotkey_edit.setFocus()
                return

        self._config["backend"].update(
            base_url=self.base_url_edit.text().strip(),
            api_key=self.api_key_edit.text().strip(),
            model=self.model_edit.text().strip(),
            temperature=self.temperature_spin.value(),
            max_tokens=self.max_tokens_spin.value(),
            use_system_role=self.use_system_role_cb.isChecked(),
        )
        self._config["translation"].update(
            source_lang=self.source_lang_combo.currentText(),
            target_lang=self.target_lang_combo.currentText(),
            chunk_chars=self.chunk_spin.value(),
            style=self.style_combo.currentText(),
        )
        self._config.setdefault("selection", {}).update(
            enabled=self.selection_enabled_cb.isChecked(),
            hotkey=hotkey_spec,
            min_chars=self.selection_min_chars_spin.value(),
            auto_hide_ms=self._config.get("selection", {}).get("auto_hide_ms", 0),
        )
        self.accept()

    def _validate_hotkey(self, _seq: QKeySequence | None) -> None:
        """实时校验热键是否可解析，给出即时反馈。"""
        spec = self.selection_hotkey_edit.keySequence().toString(
            QKeySequence.SequenceFormat.PortableText
        )
        if not spec:
            self._hotkey_validation.setText("请输入全局热键，例如 Ctrl+F4")
            self._hotkey_validation.setStyleSheet(
                "color: #e67e22; font-size: 11px;"
            )
            return
        try:
            parse_hotkey(spec)
            self._hotkey_validation.setText(f"✓ {spec}")
            self._hotkey_validation.setStyleSheet(
                "color: #27ae60; font-size: 11px;"
            )
        except HotkeyError as e:
            self._hotkey_validation.setText(f"✗ {e}")
            self._hotkey_validation.setStyleSheet(
                "color: #c0392b; font-size: 11px;"
            )

    def _on_test(self) -> None:
        url = self.base_url_edit.text().strip()
        model = self.model_edit.text().strip()
        if not url or not model:
            self.test_result.setText("请先填写 Base URL 和模型名称。")
            self.test_result.setStyleSheet("color: #c0392b;")
            return

        # 清理旧 worker（等待退出 + deleteLater）
        self._cleanup_test_worker()

        self.test_btn.setEnabled(False)
        self.test_result.setText("测试中…")
        self.test_result.setStyleSheet("color: #888;")
        # 后台线程执行，避免后端慢时冻结对话框
        self._test_worker = TestConnectionWorker(
            url,
            self.api_key_edit.text().strip(),
            model,
            use_system_role=self.use_system_role_cb.isChecked(),
        )
        self._test_worker.ok.connect(self._on_test_ok)
        self._test_worker.err.connect(self._on_test_err)
        self._test_worker.finished.connect(self._on_test_finished)
        # 线程结束后自动清理，避免 QThread destroyed while running
        self._test_worker.finished.connect(self._test_worker.deleteLater)
        self._test_worker.start()

    def _on_test_ok(self, reply: str) -> None:
        self.test_result.setText(
            f"✅ 连接成功，模型回应：{reply.strip()[:40] or '(空)'}"
        )
        self.test_result.setStyleSheet("color: #27ae60;")

    def _on_test_err(self, message: str) -> None:
        self.test_result.setText(f"❌ {message}")
        self.test_result.setStyleSheet("color: #c0392b;")

    def _on_test_finished(self) -> None:
        self.test_btn.setEnabled(True)

    def _cleanup_test_worker(self) -> None:
        """安全清理旧测试线程：等待退出 + 断开信号 + deleteLater。"""
        w = self._test_worker
        if w is None:
            return
        if w.isRunning() and not w.wait(3000):
            logger.warning("测试连接线程 3s 未退出，放弃等待")
        try:
            w.ok.disconnect()
            w.err.disconnect()
            w.finished.disconnect()
        except (TypeError, RuntimeError):
            pass
        # deleteLater 由 finished 信号触发；若线程已结束则立即调度
        w.deleteLater()
        self._test_worker = None

    def closeEvent(self, event) -> None:
        # 安全清理测试线程，避免 QThread 析构时仍在运行
        self._cleanup_test_worker()
        super().closeEvent(event)
