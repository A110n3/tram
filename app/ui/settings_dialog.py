"""设置对话框：后端连接配置 + 翻译参数 + 连接测试。"""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ..core.backend import BackendError, OpenAIBackend, test_connection

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

        bf.addRow("后端预设", self.preset_combo)
        bf.addRow("Base URL", self.base_url_edit)
        bf.addRow("API Key", self.api_key_edit)
        bf.addRow("模型名称", self.model_edit)

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

        tf.addRow("温度", self.temperature_spin)
        tf.addRow("最大输出 tokens", self.max_tokens_spin)
        tf.addRow("分块长度", self.chunk_spin)
        tf.addRow("翻译风格", self.style_combo)

        layout.addWidget(trans_box)

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
        self.temperature_spin.setValue(float(b.get("temperature", 0.2)))
        self.max_tokens_spin.setValue(int(b.get("max_tokens", 2048)))
        self.chunk_spin.setValue(int(t.get("chunk_chars", 2000)))
        style = t.get("style", "忠实原文")
        if style in self.style_combo:
            self.style_combo.setCurrentText(style)

    def _on_preset(self) -> None:
        url = self.preset_combo.currentData()
        if url:
            self.base_url_edit.setText(url)

    def _on_save(self) -> None:
        if not self.model_edit.text().strip():
            QMessageBox.warning(self, "提示", "请填写模型名称。")
            return
        self._config["backend"].update(
            base_url=self.base_url_edit.text().strip(),
            api_key=self.api_key_edit.text().strip(),
            model=self.model_edit.text().strip(),
            temperature=self.temperature_spin.value(),
            max_tokens=self.max_tokens_spin.value(),
        )
        self._config["translation"].update(
            chunk_chars=self.chunk_spin.value(),
            style=self.style_combo.currentText(),
        )
        self.accept()

    def _on_test(self) -> None:
        url = self.base_url_edit.text().strip()
        model = self.model_edit.text().strip()
        if not url or not model:
            self.test_result.setText("请先填写 Base URL 和模型名称。")
            self.test_result.setStyleSheet("color: #c0392b;")
            return
        self.test_btn.setEnabled(False)
        self.test_result.setText("测试中…")
        self.test_result.setStyleSheet("color: #888;")
        # 简单同步测试（请求很快，不阻塞太久）
        try:
            reply = test_connection(
                url, self.api_key_edit.text().strip(), model
            )
            self.test_result.setText(
                f"✅ 连接成功，模型回应：{reply.strip()[:40] or '(空)'}"
            )
            self.test_result.setStyleSheet("color: #27ae60;")
        except BackendError as e:
            self.test_result.setText(f"❌ {e}")
            self.test_result.setStyleSheet("color: #c0392b;")
        finally:
            self.test_btn.setEnabled(True)
