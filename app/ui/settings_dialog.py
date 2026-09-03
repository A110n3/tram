"""设置对话框：后端连接配置 + 翻译参数 + 连接测试 + 一键获取模型。"""

from __future__ import annotations

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
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from ..config import get_default
from ..core.hotkey import HotkeyError, parse_hotkey, test_hotkey_available
from ..core.prompts import SOURCE_LANGS, TARGET_LANGS, build_default_system_prompt
from .worker import ListModelsWorker, TestConnectionWorker
from .worker_util import launch_worker, shutdown_worker

# 常用本地后端预设
PRESETS = {
    "Ollama": "http://localhost:11434/v1",
    "LM Studio": "http://localhost:1234/v1",
    "vLLM": "http://localhost:8000/v1",
}


class SettingsDialog(QDialog):
    def __init__(self, config: dict, parent=None):
        super().__init__(parent)
        # 在任务栏显示窗口，避免被其他窗口遮挡后无法呼出
        self.setWindowFlags(Qt.WindowType.Window)
        self._config = config
        self.setWindowTitle("设置")
        self.setMinimumWidth(460)
        self._test_worker: TestConnectionWorker | None = None
        self._fetch_worker: ListModelsWorker | None = None
        self._build_ui()
        self._load_values()

    # ---------- UI ----------
    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        # 创建标签页控件
        tabs = QTabWidget()
        tabs.addTab(self._build_model_tab(), "模型设置")
        tabs.addTab(self._build_translation_tab(), "翻译设置")
        tabs.addTab(self._build_hotkey_tab(), "热键设置")
        tabs.addTab(self._build_window_tab(), "窗口设置")
        layout.addWidget(tabs)

        # 保存/取消按钮
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._on_save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _build_model_tab(self) -> QWidget:
        """构建模型设置标签页。"""
        widget = QWidget()
        layout = QFormLayout(widget)
        layout.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self.preset_combo = QComboBox()
        self.preset_combo.addItem("自定义", "")
        for name, url in PRESETS.items():
            self.preset_combo.addItem(name, url)
        self.preset_combo.currentIndexChanged.connect(self._on_preset)

        self.base_url_edit = QLineEdit()
        self.api_key_edit = QLineEdit()
        self.api_key_edit.setEchoMode(QLineEdit.EchoMode.Password)

        # 可编辑下拉框：既能从后端拉取模型列表选择，也支持手动输入
        self.model_combo = QComboBox()
        self.model_combo.setEditable(True)
        self.model_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.model_combo.setPlaceholderText("例如 qwen2.5:7b / llama3.1:8b")
        self.fetch_btn = QPushButton("获取模型")
        self.fetch_btn.setToolTip(
            "调用后端 OpenAI 兼容 /models 接口，一键获取可用模型列表"
        )
        self.fetch_btn.clicked.connect(self._on_fetch_models)
        model_row = QHBoxLayout()
        model_row.addWidget(self.model_combo, 1)
        model_row.addWidget(self.fetch_btn)
        self.fetch_result = QLabel("")
        self.fetch_result.setWordWrap(True)

        self.use_system_role_cb = QCheckBox("使用 system 消息")
        self.use_system_role_cb.setToolTip(
            "翻译指令以 system 角色消息发送。部分后端不支持 system 消息"
            "（请求会返回 5xx 错误），遇到翻译报错 500/502 时可取消勾选，"
            "改为将指令并入用户消息发送。"
        )

        layout.addRow("后端预设", self.preset_combo)
        layout.addRow("Base URL", self.base_url_edit)
        layout.addRow("API Key", self.api_key_edit)
        layout.addRow("模型名称", model_row)
        layout.addRow("", self.fetch_result)
        layout.addRow("", self.use_system_role_cb)

        # 测试连接
        test_row = QHBoxLayout()
        self.test_btn = QPushButton("测试连接")
        self.test_btn.clicked.connect(self._on_test)
        self.test_result = QLabel("")
        self.test_result.setWordWrap(True)
        test_row.addWidget(self.test_btn)
        test_row.addWidget(self.test_result, 1)
        layout.addRow("", test_row)

        return widget

    def _build_translation_tab(self) -> QWidget:
        """构建翻译设置标签页。"""
        widget = QWidget()
        layout = QFormLayout(widget)
        layout.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self.source_lang_combo = QComboBox()
        self.source_lang_combo.addItems(SOURCE_LANGS)
        self.source_lang_combo.setToolTip(
            "选择源语言可辅助翻译。默认自动识别，由模型自行判断。"
        )

        self.target_lang_combo = QComboBox()
        self.target_lang_combo.addItems(TARGET_LANGS)

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

        self.custom_prompt_btn = QPushButton("自定义提示词")
        self.custom_prompt_btn.setToolTip(
            "配置自定义系统提示词。留空则使用默认模板。"
        )
        self.custom_prompt_btn.clicked.connect(self._on_edit_custom_prompt)
        self.custom_prompt_status = QLabel("")
        self.custom_prompt_status.setStyleSheet("color: #888; font-size: 11px;")

        layout.addRow("源语言", self.source_lang_combo)
        layout.addRow("目标语言", self.target_lang_combo)
        layout.addRow("温度", self.temperature_spin)
        layout.addRow("最大输出 tokens", self.max_tokens_spin)
        layout.addRow("分块长度", self.chunk_spin)
        layout.addRow("翻译风格", self.style_combo)
        layout.addRow("", self.custom_prompt_btn)
        layout.addRow("", self.custom_prompt_status)

        return widget

    def _build_hotkey_tab(self) -> QWidget:
        """构建热键设置标签页。"""
        widget = QWidget()
        layout = QVBoxLayout(widget)

        # 划词翻译分组
        sel_gb = QGroupBox("划词翻译")
        sl = QFormLayout(sel_gb)
        sl.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self.selection_enabled_cb = QCheckBox("启用划词翻译")
        self.selection_hotkey_edit = QKeySequenceEdit()
        self.selection_hotkey_edit.keySequenceChanged.connect(self._validate_hotkey)
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

        # OCR 识图翻译分组
        ocr_gb = QGroupBox("OCR 识图翻译")
        ol = QFormLayout(ocr_gb)
        ol.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self.ocr_enabled_cb = QCheckBox("启用 OCR 识图翻译")
        self.ocr_hotkey_edit = QKeySequenceEdit()
        self.ocr_hotkey_edit.keySequenceChanged.connect(self._validate_ocr_hotkey)
        self._ocr_hotkey_validation = QLabel("")
        self._ocr_hotkey_validation.setStyleSheet("color: #888; font-size: 11px;")

        ol.addRow("", self.ocr_enabled_cb)
        ol.addRow("全局热键", self.ocr_hotkey_edit)
        ol.addRow("", self._ocr_hotkey_validation)

        layout.addWidget(ocr_gb)
        layout.addStretch()

        return widget

    def _build_window_tab(self) -> QWidget:
        """构建窗口设置标签页（浮窗外观与交互）。"""
        widget = QWidget()
        layout = QVBoxLayout(widget)

        # 外观分组
        appearance_gb = QGroupBox("外观")
        al = QFormLayout(appearance_gb)
        al.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self.popup_opacity_spin = QDoubleSpinBox()
        self.popup_opacity_spin.setRange(0.3, 1.0)
        self.popup_opacity_spin.setSingleStep(0.1)
        self.popup_opacity_spin.setDecimals(2)
        self.popup_opacity_spin.setToolTip(
            "悬浮窗整体不透明度，越低越透明（低于 0.3 文字难以阅读）"
        )

        al.addRow("浮窗不透明度", self.popup_opacity_spin)
        layout.addWidget(appearance_gb)

        # 交互分组
        interact_gb = QGroupBox("交互")
        il = QFormLayout(interact_gb)
        il.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self.auto_hide_combo = QComboBox()
        self.auto_hide_combo.addItem("失焦自动隐藏", 0)
        self.auto_hide_combo.addItem("3 秒", 3000)
        self.auto_hide_combo.addItem("5 秒", 5000)
        self.auto_hide_combo.addItem("10 秒", 10_000)
        self.auto_hide_combo.addItem("30 秒", 30_000)
        self.auto_hide_combo.addItem("永不自动隐藏", -1)
        self.auto_hide_combo.setToolTip(
            "悬浮窗自动隐藏方式：失焦隐藏 / 固定时长后自动隐藏 / 常驻不隐藏"
        )

        self.popup_click_through_cb = QCheckBox("启用鼠标穿透")
        self.popup_click_through_cb.setToolTip(
            "穿透后点击浮窗直达底层窗口，浮窗不可拖动/复制/关闭，\n"
            "适合游戏/视频等场景做字幕条用；失焦隐藏退化为 10 秒自动隐藏"
        )

        il.addRow("自动隐藏", self.auto_hide_combo)
        il.addRow("", self.popup_click_through_cb)
        layout.addWidget(interact_gb)
        layout.addStretch()

        return widget

    # ---------- 数据 ----------
    def _load_values(self) -> None:
        b = self._config.get("backend", {})
        t = self._config.get("translation", {})

        url = b.get("base_url", get_default("backend", "base_url"))
        idx = self.preset_combo.findData(url)
        if idx >= 0:
            self.preset_combo.setCurrentIndex(idx)
        self.base_url_edit.setText(url)
        self.api_key_edit.setText(b.get("api_key", get_default("backend", "api_key")))
        self.model_combo.setCurrentText(b.get("model", get_default("backend", "model")))
        self.use_system_role_cb.setChecked(
            bool(b.get("use_system_role", get_default("backend", "use_system_role")))
        )
        self.temperature_spin.setValue(
            float(b.get("temperature", get_default("backend", "temperature")))
        )
        self.max_tokens_spin.setValue(
            int(b.get("max_tokens", get_default("backend", "max_tokens")))
        )
        self.chunk_spin.setValue(
            int(t.get("chunk_chars", get_default("translation", "chunk_chars")))
        )
        source_lang = t.get("source_lang", get_default("translation", "source_lang"))
        if self.source_lang_combo.findText(source_lang) >= 0:
            self.source_lang_combo.setCurrentText(source_lang)
        target_lang = t.get("target_lang", get_default("translation", "target_lang"))
        if self.target_lang_combo.findText(target_lang) >= 0:
            self.target_lang_combo.setCurrentText(target_lang)
        style = t.get("style", get_default("translation", "style"))
        if self.style_combo.findText(style) >= 0:
            self.style_combo.setCurrentText(style)

        # 更新自定义提示词状态显示
        self._update_custom_prompt_status()

        sel = self._config.get("selection", {})
        self.selection_enabled_cb.setChecked(
            sel.get("enabled", get_default("selection", "enabled"))
        )
        self.selection_hotkey_edit.setKeySequence(
            QKeySequence.fromString(
                sel.get("hotkey", get_default("selection", "hotkey")),
                QKeySequence.SequenceFormat.PortableText,
            )
        )
        self.selection_min_chars_spin.setValue(
            int(sel.get("min_chars", get_default("selection", "min_chars")))
        )
        # 初始校验当前热键
        self._validate_hotkey(None)

        # 窗口设置
        self.popup_opacity_spin.setValue(
            float(sel.get("popup_opacity", get_default("selection", "popup_opacity")))
        )
        self.popup_click_through_cb.setChecked(
            bool(
                sel.get(
                    "popup_click_through",
                    get_default("selection", "popup_click_through"),
                )
            )
        )
        auto_hide_ms = int(
            sel.get("auto_hide_ms", get_default("selection", "auto_hide_ms"))
        )
        idx = self.auto_hide_combo.findData(auto_hide_ms)
        if idx < 0:
            # 配置值不在预设列表中，添加自定义项
            self.auto_hide_combo.addItem(f"{auto_hide_ms} ms", auto_hide_ms)
            idx = self.auto_hide_combo.findData(auto_hide_ms)
        self.auto_hide_combo.setCurrentIndex(idx)

        ocr = self._config.get("ocr", {})
        self.ocr_enabled_cb.setChecked(ocr.get("enabled", get_default("ocr", "enabled")))
        self.ocr_hotkey_edit.setKeySequence(
            QKeySequence.fromString(
                ocr.get("hotkey", get_default("ocr", "hotkey")),
                QKeySequence.SequenceFormat.PortableText,
            )
        )
        self._validate_ocr_hotkey(None)

    def _on_preset(self) -> None:
        url = self.preset_combo.currentData()
        if url:
            self.base_url_edit.setText(url)

    def _on_save(self) -> None:
        if not self.model_combo.currentText().strip():
            QMessageBox.warning(self, "提示", "请填写模型名称。")
            return

        # (配置节名, 热键串, 输入框, 校验标签)：两处热键统一走同一校验流程
        hotkeys = (
            (
                "selection",
                self._hotkey_spec(self.selection_hotkey_edit),
                self.selection_hotkey_edit,
                self._hotkey_validation,
            ),
            (
                "ocr",
                self._hotkey_spec(self.ocr_hotkey_edit),
                self.ocr_hotkey_edit,
                self._ocr_hotkey_validation,
            ),
        )

        # 格式校验 + 空值拦截（清空热键框不是合法输入，不再静默回退默认）
        for _section, spec, edit, label in hotkeys:
            if not spec:
                self._reject_hotkey(label, "请输入全局热键", edit)
                return
            try:
                parse_hotkey(spec)
            except HotkeyError as e:
                self._reject_hotkey(label, str(e), edit)
                return

        # 重复检测按解析结果两两比较："Ctrl+F4" 与 "Control+F4" 等价
        # 写法字符串比较会漏拦，运行时第二个注册才失败
        names_cn = {"selection": "划词", "ocr": "OCR"}
        for i, (_section, spec, edit, label) in enumerate(hotkeys):
            for prev_section, prev_spec, _e, _l in hotkeys[:i]:
                if parse_hotkey(spec) == parse_hotkey(prev_spec):
                    self._reject_hotkey(
                        label,
                        f"与{names_cn[prev_section]}热键重复，请更换组合键",
                        edit,
                    )
                    return

        # 可注册性校验：与当前已生效配置等价则跳过
        # （自身热键线程正在注册同一组合，试注册必然冲突，是误报）
        for section, spec, edit, label in hotkeys:
            if self._hotkey_changed(spec, section):
                ok, err = test_hotkey_available(spec)
                if not ok:
                    self._reject_hotkey(label, err, edit)
                    return
        hotkey_spec = hotkeys[0][1]
        ocr_hotkey_spec = hotkeys[1][1]

        self._config.setdefault("backend", {}).update(
            base_url=self.base_url_edit.text().strip(),
            api_key=self.api_key_edit.text().strip(),
            model=self.model_combo.currentText().strip(),
            temperature=self.temperature_spin.value(),
            max_tokens=self.max_tokens_spin.value(),
            use_system_role=self.use_system_role_cb.isChecked(),
        )
        self._config.setdefault("translation", {}).update(
            source_lang=self.source_lang_combo.currentText(),
            target_lang=self.target_lang_combo.currentText(),
            chunk_chars=self.chunk_spin.value(),
            style=self.style_combo.currentText(),
            custom_prompt=self._config.get("translation", {}).get(
                "custom_prompt", get_default("translation", "custom_prompt")
            ),
        )
        sel = self._config.setdefault("selection", {})
        sel.update(
            enabled=self.selection_enabled_cb.isChecked(),
            hotkey=hotkey_spec,
            min_chars=self.selection_min_chars_spin.value(),
            auto_hide_ms=int(self.auto_hide_combo.currentData()),
            popup_opacity=self.popup_opacity_spin.value(),
            popup_click_through=self.popup_click_through_cb.isChecked(),
        )
        # languages/min_chars 为内部保留字段（config.json 手改），保留原值
        ocr = self._config.setdefault("ocr", {})
        ocr.update(
            enabled=self.ocr_enabled_cb.isChecked(),
            hotkey=ocr_hotkey_spec,
            languages=ocr.get("languages", get_default("ocr", "languages")),
            min_chars=ocr.get("min_chars", get_default("ocr", "min_chars")),
        )
        self.accept()

    # ---------- 热键校验 ----------
    @staticmethod
    def _hotkey_spec(edit: QKeySequenceEdit) -> str:
        return edit.keySequence().toString(QKeySequence.SequenceFormat.PortableText)

    @staticmethod
    def _reject_hotkey(
        label: QLabel, msg: str, edit: QKeySequenceEdit
    ) -> None:
        """保存失败：在对应校验标签上显示错误并聚焦热键输入框。"""
        label.setText(f"✗ {msg}")
        label.setStyleSheet("color: #c0392b; font-size: 11px;")
        edit.setFocus()

    def _hotkey_changed(self, spec: str, section: str) -> bool:
        """新热键是否与当前已生效配置不等价（按解析结果比较）。"""
        current = self._config.get(section, {}).get("hotkey", "")
        if not current:
            return True
        try:
            return parse_hotkey(spec) != parse_hotkey(current)
        except HotkeyError:
            return True

    def _validate_hotkey_field(
        self, edit: QKeySequenceEdit, label: QLabel, example: str
    ) -> None:
        """实时校验热键是否可解析，给出即时反馈。"""
        spec = self._hotkey_spec(edit)
        if not spec:
            label.setText(f"请输入全局热键，例如 {example}")
            label.setStyleSheet("color: #e67e22; font-size: 11px;")
            return
        try:
            parse_hotkey(spec)
        except HotkeyError as e:
            label.setText(f"✗ {e}")
            label.setStyleSheet("color: #c0392b; font-size: 11px;")
            return
        label.setText(f"✓ {spec}")
        label.setStyleSheet("color: #27ae60; font-size: 11px;")

    def _validate_hotkey(self, _seq: QKeySequence | None) -> None:
        self._validate_hotkey_field(
            self.selection_hotkey_edit, self._hotkey_validation, "Ctrl+F4"
        )

    def _validate_ocr_hotkey(self, _seq: QKeySequence | None) -> None:
        self._validate_hotkey_field(
            self.ocr_hotkey_edit, self._ocr_hotkey_validation, "Ctrl+Shift+F4"
        )

    def _on_test(self) -> None:
        url = self.base_url_edit.text().strip()
        model = self.model_combo.currentText().strip()
        if not url or not model:
            self.test_result.setText("请先填写 Base URL 和模型名称。")
            self.test_result.setStyleSheet("color: #c0392b;")
            return

        # 清理旧 worker（等待退出 + deleteLater）
        shutdown_worker(self, "_test_worker", "测试连接")

        self.test_btn.setEnabled(False)
        self.test_result.setText("测试中…")
        self.test_result.setStyleSheet("color: #888;")
        # 后台线程执行，避免后端慢时冻结对话框
        self._test_worker = TestConnectionWorker(
            url,
            self.api_key_edit.text().strip(),
            model,
            use_system_role=self.use_system_role_cb.isChecked(),
            parent=self,
        )
        launch_worker(
            self,
            "_test_worker",
            self._test_worker,
            on_ok=self._on_test_ok,
            on_err=self._on_test_err,
            on_finished=self._on_test_finished,
        )

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

    # ---------- 获取模型 ----------
    def _on_fetch_models(self) -> None:
        url = self.base_url_edit.text().strip()
        if not url:
            self.fetch_result.setText("请先填写 Base URL。")
            self.fetch_result.setStyleSheet("color: #c0392b;")
            return

        # 清理旧 worker（等待退出 + deleteLater）
        shutdown_worker(self, "_fetch_worker", "获取模型")

        self.fetch_btn.setEnabled(False)
        self.fetch_result.setText("获取中…")
        self.fetch_result.setStyleSheet("color: #888;")
        # 后台线程执行，避免后端慢时冻结对话框
        self._fetch_worker = ListModelsWorker(
            url, self.api_key_edit.text().strip(), parent=self
        )
        launch_worker(
            self,
            "_fetch_worker",
            self._fetch_worker,
            on_ok=self._on_fetch_ok,
            on_err=self._on_fetch_err,
            on_finished=self._on_fetch_finished,
        )

    def _on_fetch_ok(self, models: list[str]) -> None:
        if not models:
            self.fetch_result.setText(
                "⚠ 后端未返回任何模型，请手动填写模型名称。"
            )
            self.fetch_result.setStyleSheet("color: #e67e22;")
            return

        current = self.model_combo.currentText().strip()
        self.model_combo.clear()
        self.model_combo.addItems(models)
        if current:
            idx = self.model_combo.findText(current)
            if idx >= 0:
                self.model_combo.setCurrentIndex(idx)
            else:
                # 已填模型不在列表中，保留手动输入的内容
                self.model_combo.setCurrentText(current)
        self.fetch_result.setText(f"✅ 获取到 {len(models)} 个模型")
        self.fetch_result.setStyleSheet("color: #27ae60;")

    def _on_fetch_err(self, message: str) -> None:
        self.fetch_result.setText(f"❌ {message}（可手动填写模型名称）")
        self.fetch_result.setStyleSheet("color: #c0392b;")

    def _on_fetch_finished(self) -> None:
        self.fetch_btn.setEnabled(True)

    # ---------- 自定义提示词 ----------
    def _update_custom_prompt_status(self) -> None:
        """更新自定义提示词状态标签。"""
        custom_prompt = self._config.get("translation", {}).get(
            "custom_prompt", get_default("translation", "custom_prompt")
        )
        if custom_prompt.strip():
            self.custom_prompt_status.setText("✓ 已启用自定义提示词")
            self.custom_prompt_status.setStyleSheet("color: #27ae60; font-size: 11px;")
        else:
            self.custom_prompt_status.setText("使用默认提示词模板")
            self.custom_prompt_status.setStyleSheet("color: #888; font-size: 11px;")

    def _on_edit_custom_prompt(self) -> None:
        """打开自定义提示词编辑对话框。"""
        current = self._config.get("translation", {}).get(
            "custom_prompt", get_default("translation", "custom_prompt")
        )
        # 按当前标签页的语言/风格渲染内置模板，供预填充与一键恢复
        default_prompt = build_default_system_prompt(
            target_lang=self.target_lang_combo.currentText(),
            source_lang=self.source_lang_combo.currentText(),
            style=self.style_combo.currentText(),
        )
        dialog = CustomPromptDialog(current, default_prompt, parent=self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            new_prompt = dialog.get_prompt()
            # 内容与内置模板一致时视为恢复默认：清空自定义提示词，
            # 回到动态模板——术语表/上下文仍随翻译自动注入，语言/风格
            # 后续改动继续生效，避免把当前参数固化进提示词
            if new_prompt == default_prompt:
                new_prompt = ""
            self._config.setdefault("translation", {})["custom_prompt"] = new_prompt
            self._update_custom_prompt_status()

    def closeEvent(self, event) -> None:
        # 安全清理后台线程，避免 QThread 析构时仍在运行
        shutdown_worker(self, "_test_worker", "测试连接")
        shutdown_worker(self, "_fetch_worker", "获取模型")
        super().closeEvent(event)


class CustomPromptDialog(QDialog):
    """自定义提示词编辑对话框。

    未设置自定义提示词时，输入框预填充按当前语言/风格渲染的内置
    默认模板，用户可直接在其基础上修改；「恢复默认值」一键填回。
    """

    def __init__(self, current_prompt: str, default_prompt: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("自定义系统提示词")
        self.setMinimumSize(600, 400)
        self._default_prompt = default_prompt
        self._build_ui(current_prompt.strip() or default_prompt)

    def _build_ui(self, initial_prompt: str) -> None:
        layout = QVBoxLayout(self)

        hint = QLabel(
            "自定义系统提示词将原样替代默认模板（术语表、上下文等运行时参数不再自动注入）；"
            "留空则使用内置模板（源语言、目标语言、翻译风格、术语表、上下文自动生效）。"
            "未自定义时输入框预填充内置模板，可直接修改。\n\n"
            "提示：自定义提示词中可以使用 {text} 占位符引用待翻译文本，"
            "但通常不需要（系统会自动将文本作为用户消息发送）。"
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #888; font-size: 11px; padding: 8px;")
        layout.addWidget(hint)

        self.prompt_edit = QTextEdit()
        self.prompt_edit.setPlaceholderText(
            "输入自定义系统提示词，例如：\n\n"
            "You are a professional translator. Translate the following text "
            "to Chinese, keeping the original tone and style."
        )
        self.prompt_edit.setPlainText(initial_prompt)
        layout.addWidget(self.prompt_edit)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        restore_btn = buttons.addButton(
            "恢复默认值", QDialogButtonBox.ButtonRole.ResetRole
        )
        assert restore_btn is not None  # addButton(str, role) 总是创建成功
        restore_btn.clicked.connect(self._on_restore_default)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _on_restore_default(self) -> None:
        """一键填回内置默认提示词（按打开对话框时的语言/风格渲染）。"""
        self.prompt_edit.setPlainText(self._default_prompt)

    def get_prompt(self) -> str:
        """返回编辑后的提示词（去除首尾空白）。"""
        return self.prompt_edit.toPlainText().strip()
