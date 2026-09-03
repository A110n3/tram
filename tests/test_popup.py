"""TranslationPopup 锚定与定位行为测试。

核心回归：同一弹窗实例要经历多次状态切换（识别中 -> 翻译中 ->
完成/错误/缓存重显），窗口已可见时不得跟随光标重新定位——
否则 OCR 识别耗时数秒期间鼠标移动后，窗口会"瞬移"到新位置，
看起来像关闭重开。
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QPoint  # noqa: E402

from app.ui import popup as popup_module  # noqa: E402
from app.ui.popup import TranslationPopup  # noqa: E402


class _FakeCursor:
    """QCursor 替身：pos() 返回可编程位置，避免依赖真实鼠标。"""

    pos_value = QPoint(100, 100)

    @staticmethod
    def pos() -> QPoint:
        return _FakeCursor.pos_value


@pytest.fixture
def qapp():
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


@pytest.fixture
def cursor(monkeypatch):
    monkeypatch.setattr(popup_module, "QCursor", _FakeCursor)
    return _FakeCursor


def _center(qapp) -> QPoint:
    return qapp.primaryScreen().availableGeometry().center()


def test_first_show_anchors_at_cursor(qapp, cursor):
    """未显示过的窗口：首次显示定位在光标右下方。"""
    c = _center(qapp)
    cursor.pos_value = c
    popup = TranslationPopup()
    popup.show_loading("识别中…")
    assert popup.isVisible()
    # 光标放在屏幕中央：无边界翻转/夹紧，位置即固定偏移 (16, 20)
    assert popup.x() == c.x() + 16
    assert popup.y() == c.y() + 20


def test_visible_popup_keeps_position_across_states(qapp, cursor):
    """核心回归：已显示的窗口状态切换时不跟随光标重新定位。"""
    cursor.pos_value = _center(qapp)
    popup = TranslationPopup()
    popup.show_loading("识别中…")
    assert popup.isVisible()
    anchored = popup.pos()

    # 模拟识别/取词期间鼠标移到屏幕另一角（该位置会触发边界翻转）
    geo = qapp.primaryScreen().availableGeometry()
    cursor.pos_value = QPoint(geo.right() - 10, geo.bottom() - 10)

    # OCR 识别完成进入翻译阶段 / 翻译失败 / 重复文本缓存重显 / 无结果淡出
    popup.show_loading("翻译中…", can_retry=True)
    assert popup.pos() == anchored
    popup.show_error("后端连接失败")
    assert popup.pos() == anchored
    popup.show_cached("缓存译文")
    assert popup.pos() == anchored
    popup.fade_out("未识别到文字")
    assert popup.pos() == anchored


def test_hidden_popup_reshows_at_cursor(qapp, cursor):
    """失焦隐藏后重新出现的窗口跟随当前光标（重新锚定）。"""
    cursor.pos_value = _center(qapp)
    popup = TranslationPopup()
    popup.show_loading("识别中…")
    popup.hide()
    assert not popup.isVisible()

    c2 = _center(qapp) - QPoint(200, 200)
    cursor.pos_value = c2
    popup.show_error("失败")
    assert popup.pos() == QPoint(c2.x() + 16, c2.y() + 20)


def test_growth_keeps_window_on_screen(qapp, cursor):
    """内容增长不重定位，但超出屏幕的部分收回可视区域。"""
    geo = qapp.primaryScreen().availableGeometry()
    # 光标贴近屏幕底边：小加载窗翻转到光标上方，长译文将撑出屏幕
    cursor.pos_value = QPoint(geo.center().x(), geo.bottom() - 20)
    popup = TranslationPopup()
    popup.show_loading("翻译中…")
    assert popup.isVisible()
    x0 = popup.x()

    popup.set_translation("hello world " * 30)  # 长译文触发高度增长
    assert popup.x() == x0  # 锚点不横向移动
    # 窗口完整收回屏幕可视区域内
    assert popup.y() >= geo.top()
    assert popup.y() + popup.height() - 1 <= geo.bottom()


# ---------- 不透明度 ----------
def test_opacity_default_is_one(qapp):
    """默认不透明度为 1.0（完全不透明）。"""
    popup = TranslationPopup()
    assert popup.windowOpacity() == pytest.approx(1.0, abs=0.01)


def test_opacity_setter_clamps_to_min(qapp):
    """不透明度低于下限时夹紧到 _MIN_OPACITY。"""
    from app.ui.popup import _MIN_OPACITY

    popup = TranslationPopup()
    popup.set_opacity(0.1)  # 远低于下限
    assert popup.windowOpacity() == pytest.approx(_MIN_OPACITY, abs=0.01)


def test_opacity_setter_clamps_to_one(qapp):
    """不透明度高于 1.0 时夹紧到 1.0。"""
    popup = TranslationPopup()
    popup.set_opacity(1.5)
    assert popup.windowOpacity() == pytest.approx(1.0, abs=0.01)


def test_opacity_constructor_parameter(qapp):
    """构造函数可传入初始不透明度。"""
    popup = TranslationPopup(opacity=0.7)
    assert popup.windowOpacity() == pytest.approx(0.7, abs=0.01)


# ---------- 鼠标穿透 ----------
def test_click_through_default_off(qapp):
    """默认不启用鼠标穿透。"""
    popup = TranslationPopup()
    assert not popup._click_through
    from PyQt6.QtCore import Qt

    assert not (popup.windowFlags() & Qt.WindowType.WindowTransparentForInput)


def test_click_through_enables_window_flag(qapp):
    """启用穿透时设置 WindowTransparentForInput 窗口标志。"""
    from PyQt6.QtCore import Qt

    popup = TranslationPopup()
    popup.set_click_through(True)
    assert popup._click_through
    assert bool(popup.windowFlags() & Qt.WindowType.WindowTransparentForInput)


def test_click_through_hides_header(qapp):
    """穿透模式隐藏标题栏控件（拖动提示、关闭、重试）。"""
    popup = TranslationPopup()
    popup.show_loading("测试")
    assert popup._drag_hint.isVisible()
    assert popup._close_btn.isVisible()

    popup.set_click_through(True)
    assert not popup._drag_hint.isVisible()
    assert not popup._close_btn.isVisible()
    assert not popup._retry_btn.isVisible()


def test_click_through_disables_text_selection(qapp):
    """穿透模式禁用文本选中（选中无意义且可能干扰事件传递）。"""
    from PyQt6.QtCore import Qt

    popup = TranslationPopup()
    # 非穿透模式：可选中文本
    assert bool(
        popup._target_label.textInteractionFlags()
        & Qt.TextInteractionFlag.TextSelectableByMouse
    )

    popup.set_click_through(True)
    # 穿透模式：无交互
    assert (
        popup._target_label.textInteractionFlags()
        == Qt.TextInteractionFlag.NoTextInteraction
    )


def test_click_through_reduces_chrome_height(qapp):
    """穿透模式 chrome 高度减少（标题栏隐藏）。"""
    from app.ui.popup import _CHROME_HEIGHT, _CHROME_NO_HEADER

    popup = TranslationPopup()
    popup.show()
    popup.set_translation("行1\n行2\n行3\n行4\n行5\n行6")
    h_normal = popup.height()

    popup.set_click_through(True)
    h_through = popup.height()

    # 中等内容（不触顶不触底）时，高度差约等于 chrome 减少量
    assert h_normal - h_through == pytest.approx(
        _CHROME_HEIGHT - _CHROME_NO_HEADER, abs=4
    )


def test_click_through_preserves_visibility(qapp):
    """切换穿透模式时窗口保持可见（setWindowFlags 会隐藏窗口，需手动恢复）。"""
    popup = TranslationPopup()
    popup.show_loading("测试")
    assert popup.isVisible()

    popup.set_click_through(True)
    assert popup.isVisible()

    popup.set_click_through(False)
    assert popup.isVisible()


def test_click_through_fallback_hide_timer(qapp):
    """穿透模式 + 失焦隐藏配置时，退化为 _CLICK_THROUGH_HIDE_MS 兜底。"""
    from app.ui.popup import _CLICK_THROUGH_HIDE_MS

    # auto_hide_ms=0 表示失焦隐藏
    popup = TranslationPopup(auto_hide_ms=0, click_through=True)
    popup.show_loading("测试")
    # 计时器应已启动，间隔等于兜底时长
    assert popup._timer.isActive()
    assert popup._timer.interval() == _CLICK_THROUGH_HIDE_MS


def test_click_through_with_explicit_timeout(qapp):
    """穿透模式 + 显式超时时，使用配置的超时时长。"""
    popup = TranslationPopup(auto_hide_ms=5000, click_through=True)
    popup.show_loading("测试")
    assert popup._timer.isActive()
    assert popup._timer.interval() == 5000
