"""Tram 划词翻译 — 托盘常驻，热键触发取词翻译。"""

from __future__ import annotations

from collections.abc import Callable

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QAction, QActionGroup, QColor, QCursor, QFont, QIcon, QPainter, QPixmap
from PyQt6.QtWidgets import (
    QApplication,
    QLabel,
    QMainWindow,
    QMenu,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

from ..config import APP_VERSION, get_default, save_config
from ..core.prompts import TARGET_LANGS
from .glossary_dialog import GlossaryDialog
from .monitor_translator import MonitorTranslator
from .ocr_translator import OCRTranslator
from .selection_translator import SelectionTranslator
from .settings_dialog import SettingsDialog
from .worker import TestConnectionWorker
from .worker_util import drop_worker, launch_worker, shutdown_worker


class MainWindow(QMainWindow):
    def __init__(self, config: dict):
        super().__init__()
        self._config = config
        self._quitting = False

        # 对话框单例引用：防止从托盘重复打开
        self._settings_dialog: SettingsDialog | None = None
        self._glossary_dialog: GlossaryDialog | None = None

        self.setWindowTitle("关于 Tram")
        self.resize(380, 520)
        self._build_ui()

        # 划词翻译服务
        self._selection_translator = SelectionTranslator(self._config)
        self._selection_translator.hotkey_status.connect(
            lambda ok, msg: self._on_hotkey_status(ok, msg, "划词")
        )

        # OCR 识图翻译服务
        self._ocr_translator = OCRTranslator(self._config)
        self._ocr_translator.hotkey_status.connect(
            lambda ok, msg: self._on_hotkey_status(ok, msg, "OCR")
        )

        # 区域实时监控翻译服务
        self._monitor_translator = MonitorTranslator(self._config)
        self._monitor_translator.hotkey_status.connect(
            lambda ok, msg: self._on_hotkey_status(ok, msg, "监控")
        )

        # 切换目标语言后的连接测试线程
        self._lang_test_worker: TestConnectionWorker | None = None
        self._lang_test_lang: str = ""

        # 模型预热线程（启动/切换模型后触发，见 _start_warmup）
        self._warmup_worker: TestConnectionWorker | None = None

        # 各服务最新状态：合并进唯一一条托盘通知展示（见 _show_aggregated_status）
        self._statuses: dict[str, tuple[bool, str]] = {}

        # 托盘
        self._create_tray()
        self._apply_selection_config()

        # 启动划词翻译（如果已启用），热键注册结果由 _on_hotkey_status 统一通知
        if self._config.get("selection", {}).get(
            "enabled", get_default("selection", "enabled")
        ):
            self._selection_translator.start()
        else:
            self._show_aggregated_status(extra="点击托盘图标开启划词翻译")

        # 启动 OCR 识图翻译（如果已启用）
        if self._config.get("ocr", {}).get("enabled", get_default("ocr", "enabled")):
            self._ocr_translator.start()

        # 启动区域实时监控（如果已启用）
        if self._config.get("monitor", {}).get(
            "enabled", get_default("monitor", "enabled")
        ):
            self._monitor_translator.start()

        # 后台预热模型：把"等待模型加载"从用户首次翻译挪到启动阶段
        self._start_warmup()

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
        """构建「关于」信息页：应用图标、名称、版本、功能、技术信息。"""
        central = QWidget()
        central.setObjectName("AboutPage")
        lay = QVBoxLayout(central)
        lay.setContentsMargins(28, 28, 28, 28)
        lay.setSpacing(0)

        # 应用图标
        pixmap = self._make_tray_icon()
        icon_label = QLabel()
        icon_label.setPixmap(
            pixmap.scaled(
                64, 64,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )
        icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        # 应用名
        name_label = QLabel("Tram")
        name_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        name_label.setStyleSheet(
            "font-size: 24px; font-weight: bold; color: #4a9eff;"
            " margin-top: 14px;"
        )

        # 版本
        version_label = QLabel(f"v{APP_VERSION}")
        version_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        version_label.setStyleSheet(
            "font-size: 13px; color: #788090; margin-top: 4px;"
        )

        # 一句话描述
        desc_label = QLabel("离线划词/识图翻译，接入本地大模型")
        desc_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        desc_label.setStyleSheet(
            "font-size: 14px; color: #c0c4cc; margin-top: 18px;"
        )

        # 分隔线
        sep1 = self._make_separator()

        # 功能列表
        feat_title = QLabel("功能")
        feat_title.setStyleSheet(
            "font-size: 13px; font-weight: bold; color: #a0a8b4;"
            " margin-bottom: 8px;"
        )
        features = [
            "全局热键取词 — 选中文本按热键即译",
            "OCR 识图翻译 — 框选屏幕区域，识别图文即译",
            "区域实时监控 — 框选字幕区，新字幕自动识别翻译",
            "流式悬浮窗 — 译文边生成边显示",
            "系统托盘常驻 — 后台静默运行",
            "多后端切换 — Ollama / LM Studio / vLLM",
            "术语表 — 自定义专有名词映射",
        ]

        # 分隔线
        sep2 = self._make_separator()

        # 技术信息
        tech_label = QLabel("技术栈：PyQt6 · httpx · OpenAI 兼容 API")
        tech_label.setStyleSheet("font-size: 12px; color: #788090;")
        license_label = QLabel("许可证：MIT")
        license_label.setStyleSheet(
            "font-size: 12px; color: #788090; margin-top: 4px;"
        )
        author_label = QLabel("作者：A110n3")
        author_label.setStyleSheet(
            "font-size: 12px; color: #788090; margin-top: 4px;"
        )

        # 组装
        lay.addStretch()
        lay.addWidget(icon_label)
        lay.addWidget(name_label)
        lay.addWidget(version_label)
        lay.addWidget(desc_label)
        lay.addWidget(sep1)
        lay.addWidget(feat_title)
        for feat in features:
            fl = QLabel(f"  ·  {feat}")
            fl.setStyleSheet(
                "font-size: 13px; color: #c0c4cc; margin-bottom: 4px;"
            )
            lay.addWidget(fl)
        lay.addWidget(sep2)
        lay.addWidget(tech_label)
        lay.addWidget(license_label)
        lay.addWidget(author_label)
        lay.addStretch()

        self.setCentralWidget(central)

        # 深色主题背景
        self.setStyleSheet(
            "#AboutPage { background: #1e2128; }"
            "QMainWindow { background: #1e2128; }"
        )

    @staticmethod
    def _make_separator() -> QLabel:
        sep = QLabel()
        sep.setFixedHeight(1)
        sep.setStyleSheet(
            "background: rgba(255,255,255,30); margin: 20px 0;"
        )
        return sep

    # ---------- 托盘 ----------
    def _create_tray(self) -> None:
        self._tray_icon = QSystemTrayIcon(self)
        pixmap = self._make_tray_icon()
        self._tray_icon.setIcon(QIcon(pixmap))
        self._tray_icon.setToolTip("Tram 离线翻译")

        menu = QMenu()
        self._act(menu.addAction("关于 Tram"), self._show_from_tray)
        menu.addSeparator()
        self._selection_action = self._act(menu.addAction("划词翻译"), self._toggle_selection)
        self._selection_action.setCheckable(True)
        self._ocr_action = self._act(menu.addAction("OCR 识图翻译"), self._toggle_ocr)
        self._ocr_action.setCheckable(True)
        self._monitor_action = self._act(menu.addAction("区域实时监控"), self._toggle_monitor)
        self._monitor_action.setCheckable(True)

        # 目标语言快捷子菜单
        lang_menu = menu.addMenu("目标语言")
        assert lang_menu is not None
        self._lang_group = QActionGroup(self)
        self._lang_group.setExclusive(True)
        current_lang = self._config.get("translation", {}).get(
            "target_lang", get_default("translation", "target_lang")
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
        # 左键点击托盘图标时也弹出上下文菜单（与右键行为一致）
        self._tray_icon.activated.connect(self._on_tray_activated)
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

    def _on_tray_activated(
        self, reason: QSystemTrayIcon.ActivationReason
    ) -> None:
        """左键点击托盘图标时弹出上下文菜单，与右键行为一致。"""
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            menu = self._tray_icon.contextMenu()
            if menu is not None:
                menu.exec(QCursor.pos())

    def _show_aggregated_status(
        self,
        extra: str = "",
        icon: QSystemTrayIcon.MessageIcon | None = None,
    ) -> None:
        """把所有服务状态合并进唯一一条托盘通知展示。

        Windows 托盘通知会互相顶替、同一时刻只能看见一条：
        分条发送时后发的会把先发的挤没（且 msecs 参数被忽略，
        展示时长由系统管理）。合并成一条后不存在互相顶替，
        状态更新时用新内容整体替换旧通知，用户一眼看到全部状态。

        extra: 一次性附加信息（如目标语言切换结果），不记入
        _statuses，仅随本次展示。
        """
        lines = [f"{service}：{msg}" for service, (_ok, msg) in self._statuses.items()]
        if extra:
            lines.append(extra)
        if not lines:
            return
        if icon is None:
            failed = any(not ok for ok, _ in self._statuses.values())
            icon = (
                QSystemTrayIcon.MessageIcon.Warning
                if failed
                else QSystemTrayIcon.MessageIcon.Information
            )
        self._tray_icon.showMessage("Tram", "\n".join(lines), icon)

    def _toggle_selection(self) -> None:
        enabled = self._selection_action.isChecked()
        self._config.setdefault("selection", {})["enabled"] = enabled
        save_config(self._config)
        if enabled:
            # 开启结果由 hotkey_status 信号记入状态并展示
            self._selection_translator.start()
        else:
            self._selection_translator.stop()
            self._statuses["划词"] = (True, "已关闭")
            self._show_aggregated_status()
        self._apply_selection_config()

    def _toggle_ocr(self) -> None:
        enabled = self._ocr_action.isChecked()
        self._config.setdefault("ocr", {})["enabled"] = enabled
        save_config(self._config)
        if enabled:
            self._ocr_translator.start()
        else:
            self._ocr_translator.stop()
            self._statuses["OCR"] = (True, "已关闭")
            self._show_aggregated_status()
        self._apply_selection_config()

    def _toggle_monitor(self) -> None:
        enabled = self._monitor_action.isChecked()
        self._config.setdefault("monitor", {})["enabled"] = enabled
        save_config(self._config)
        if enabled:
            self._monitor_translator.start()
        else:
            # 服务关闭同时结束进行中的监控会话
            self._monitor_translator.stop()
            self._statuses["监控"] = (True, "已关闭")
            self._show_aggregated_status()
        self._apply_selection_config()

    def _on_target_lang_changed(self, action: QAction) -> None:
        """托盘菜单快捷切换目标语言。

        即时保存配置，随后后台测试模型连接，测试通过后才弹出
        切换成功通知；失败则弹出警告，避免用户以为已生效。
        """
        lang = action.text()
        self._config.setdefault("translation", {})["target_lang"] = lang
        save_config(self._config)
        # 目标语言变化后，允许立即用同一文本重新翻译验证效果
        self._selection_translator.invalidate_last_text()
        self._ocr_translator.invalidate_last_text()
        self._monitor_translator.invalidate_last_text()
        self._start_lang_test(lang)

    def _start_lang_test(self, lang: str) -> None:
        """后台测试模型连接，结果由 _on_lang_test_ok/_err 通知。"""
        drop_worker(self, "_lang_test_worker")
        self._lang_test_lang = lang
        b = self._config.get("backend", {})
        w = TestConnectionWorker(
            b.get("base_url", get_default("backend", "base_url")),
            b.get("api_key", get_default("backend", "api_key")),
            b.get("model", get_default("backend", "model")),
            use_system_role=bool(
                b.get("use_system_role", get_default("backend", "use_system_role"))
            ),
            parent=self,
        )
        self._lang_test_worker = w
        launch_worker(
            self,
            "_lang_test_worker",
            w,
            on_ok=self._on_lang_test_ok,
            on_err=self._on_lang_test_err,
        )

    def _on_lang_test_ok(self, _reply: str) -> None:
        self._lang_test_worker = None
        self._show_aggregated_status(
            extra=f"目标语言已切换为 {self._lang_test_lang}"
        )

    def _on_lang_test_err(self, message: str) -> None:
        self._lang_test_worker = None
        self._show_aggregated_status(
            extra=(
                f"目标语言已切换为 {self._lang_test_lang}，"
                f"但模型连接测试失败：\n{message.strip()[:120]}"
            ),
            icon=QSystemTrayIcon.MessageIcon.Warning,
        )

    # ---------- 模型预热 ----------
    def _start_warmup(self) -> None:
        """后台预热本地模型：发送最小请求触发后端加载模型。

        Lemonade/Ollama 等本地服务在模型未加载时，首个请求会阻塞到
        加载完成（实测数十秒，NPU 首次冷编译可达数分钟），期间浮窗
        一直显示"可能在加载模型"，用户以为卡死只能重试。启动与
        切换模型后主动预热，用户首次翻译时模型已热。

        结果静默：成功不打扰用户；失败留给真实翻译按正常管线报错
        （预热请求与翻译请求无状态关联，失败不影响后者）。
        """
        drop_worker(self, "_warmup_worker", disconnect_results=False)
        b = self._config.get("backend", {})
        w = TestConnectionWorker(
            b.get("base_url", get_default("backend", "base_url")),
            b.get("api_key", get_default("backend", "api_key")),
            b.get("model", get_default("backend", "model")),
            use_system_role=bool(
                b.get("use_system_role", get_default("backend", "use_system_role"))
            ),
            # 用配置的完整超时等待模型加载完成（默认 30s 必然不够）
            timeout=int(b.get("timeout", get_default("backend", "timeout"))),
            parent=self,
        )
        self._warmup_worker = w
        launch_worker(self, "_warmup_worker", w)

    def _apply_selection_config(self) -> None:
        """同步托盘菜单勾选状态与 tooltip，不负责启停热键或弹通知。

        热键启停由 _toggle_* / rebuild_backend / __init__ 负责，
        通知由 _on_hotkey_status 回调统一处理，避免重复弹窗。
        """
        sel = self._config.get("selection", {})
        ocr = self._config.get("ocr", {})
        mon = self._config.get("monitor", {})
        sel_enabled = sel.get("enabled", get_default("selection", "enabled"))
        ocr_enabled = ocr.get("enabled", get_default("ocr", "enabled"))
        mon_enabled = mon.get("enabled", get_default("monitor", "enabled"))
        self._selection_action.setChecked(sel_enabled)
        self._ocr_action.setChecked(ocr_enabled)
        self._monitor_action.setChecked(mon_enabled)
        sel_on = "开" if sel_enabled else "关"
        ocr_on = "开" if ocr_enabled else "关"
        mon_on = "开" if mon_enabled else "关"
        self._tray_icon.setToolTip(
            f"Tram - 划词: {sel_on} | OCR: {ocr_on} | 监控: {mon_on}"
        )

    def _on_hotkey_status(self, ok: bool, message: str, service: str) -> None:
        """热键注册结果/引导消息：记入该服务状态，合并展示。

        消息原样记录：注册失败消息自带「热键 X 注册失败」表述，
        引导类消息（如 OCR 引擎未安装）不是注册失败，不能加前缀。
        """
        self._statuses[service] = (ok, message)
        self._show_aggregated_status()

    # ---------- 菜单 ----------
    def open_settings(self) -> None:
        # 单例：已打开则呼出到最前，不重复创建
        if self._settings_dialog is not None:
            self._settings_dialog.show()
            self._settings_dialog.raise_()
            self._settings_dialog.activateWindow()
            return
        dlg = SettingsDialog(self._config, self)
        self._settings_dialog = dlg
        if dlg.exec():
            save_config(self._config)
            # 重建后端（切换模型时生效），并重新注册热键
            self._selection_translator.rebuild_backend()
            self._ocr_translator.rebuild_backend()
            self._monitor_translator.rebuild_backend()
            # 模型可能已切换，重新预热
            self._start_warmup()
            # 翻译参数可能已变化，允许立即用同一文本重新翻译验证效果
            self._selection_translator.invalidate_last_text()
            self._ocr_translator.invalidate_last_text()
            self._monitor_translator.invalidate_last_text()
            # 同步托盘菜单勾选状态与 tooltip
            self._apply_selection_config()
            # 同步托盘目标语言单选
            self._sync_target_lang_action()
        self._settings_dialog = None

    def _sync_target_lang_action(self) -> None:
        """设置保存后同步托盘菜单的目标语言选中状态。"""
        current = self._config.get("translation", {}).get(
            "target_lang", get_default("translation", "target_lang")
        )
        for action in self._lang_group.actions():
            action.setChecked(action.text() == current)

    def open_glossary(self) -> None:
        # 单例：已打开则呼出到最前，不重复创建
        if self._glossary_dialog is not None:
            self._glossary_dialog.show()
            self._glossary_dialog.raise_()
            self._glossary_dialog.activateWindow()
            return
        from ..core import glossary as gs

        dlg = GlossaryDialog(self)
        self._glossary_dialog = dlg
        if dlg.exec():
            self._config["glossary"] = gs.load_glossary()
            # 术语表变化后，允许立即用同一文本重新翻译验证效果
            self._selection_translator.invalidate_last_text()
            self._ocr_translator.invalidate_last_text()
        self._glossary_dialog = None

    # ---------- 退出 ----------
    def quit_app(self) -> None:
        self._quitting = True
        # 先取消再等待进行中的预热线程：等待模型加载可长达配置超时
        # （180s），不取消的话退出时会销毁运行中的 QThread
        shutdown_worker(self, "_warmup_worker", "预热", timeout_ms=2000)
        # 先取消再等待进行中的连接测试：测试请求超时长达 30s，
        # 不取消的话 2s 等待必然超时，退出时会销毁运行中的 QThread
        shutdown_worker(self, "_lang_test_worker", "语言测试", timeout_ms=2000)
        self._selection_translator.shutdown()
        self._ocr_translator.shutdown()
        self._monitor_translator.shutdown()
        app = QApplication.instance()
        if app is not None:
            app.quit()

    def closeEvent(self, event) -> None:
        if self._quitting:
            super().closeEvent(event)
            return
        # 关于页面关闭仅隐藏窗口，不退出应用
        # 退出由托盘菜单「退出」项负责
        event.ignore()
        self.hide()
