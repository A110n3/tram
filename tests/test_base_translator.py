"""BaseHotkeyTranslator 重试请求逻辑测试。

不启动线程、不创建浮窗：_on_retry_requested 只依赖 _pending_text
与 _cancel_workers/_begin_translation，直接替换后者记录调用。
"""

from __future__ import annotations

from app.ui.base_translator import BaseHotkeyTranslator


class _FakeTranslator(BaseHotkeyTranslator):
    section = "selection"
    service_name = "划词"
    hotkey_id = 1

    def _on_hotkey(self) -> None:
        pass


def _make() -> _FakeTranslator:
    return _FakeTranslator({"backend": {"base_url": "http://127.0.0.1:9/v1"}})


def test_retry_retranslates_pending_text():
    """有待翻译文本时：取消当前流程后用同一文本重新翻译。"""
    t = _make()
    t._pending_text = "hello"
    calls: list[str] = []
    t._begin_translation = lambda text: calls.append(text)
    t._on_retry_requested()
    assert calls == ["hello"]


def test_retry_without_pending_is_noop():
    """无待翻译文本（OCR 识别阶段/已取消）：点击重试不触发翻译。"""
    t = _make()
    calls: list[str] = []
    t._begin_translation = lambda text: calls.append(text)
    t._on_retry_requested()
    assert calls == []


def test_cancel_workers_clears_pending_text():
    """取消（新热键流程/关窗/停止）后旧文本不再待翻译。"""
    t = _make()
    t._pending_text = "hello"
    assert t._cancel_workers() is True
    assert t._pending_text == ""


def test_retry_falls_back_to_last_text():
    """完成/缓存展示态点 ✕ 后 _pending_text 已清空：
    重试回退从去重缓存取原文，仍能重新请求翻译。"""
    t = _make()
    t._pending_text = ""
    t._last_text = "cached text"
    t._last_result = "旧译文"
    calls: list[str] = []
    t._begin_translation = lambda text: calls.append(text)
    t._on_retry_requested()
    assert calls == ["cached text"]


def test_retry_clears_dedup_cache():
    """重试启动前清空去重缓存：重试在途时按热键不得重显旧译文。"""
    t = _make()
    t._pending_text = "hello"
    t._last_text = "hello"
    t._last_result = "旧译文"
    t._begin_translation = lambda text: None
    t._on_retry_requested()
    assert t._last_text == ""
    assert t._last_result == ""


def test_retry_without_any_text_is_noop():
    """_pending_text 与去重缓存均为空：点击重试不触发翻译。"""
    t = _make()
    calls: list[str] = []
    t._begin_translation = lambda text: calls.append(text)
    t._on_retry_requested()
    assert calls == []
