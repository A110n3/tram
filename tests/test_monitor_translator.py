"""MonitorTranslator 有界队列调度测试。

不启动线程、不创建小窗：TranslateWorker 换成记录构造参数的假
worker，MonitorWindow 换成记录调用的桩，只验证调度路径
（空闲即翻 / 在途排队 / 满丢最旧 / 完成后按序续翻 / 停止清队列）。
"""

from __future__ import annotations

from collections import deque

import pytest

import app.ui.monitor_translator as mt
from app.ui.monitor_translator import MonitorTranslator


class _Sig:
    """假 PyQt 信号：记录连接的槽，可手动 emit 触发。"""

    def __init__(self) -> None:
        self.slots: list = []

    def connect(self, slot) -> None:
        self.slots.append(slot)

    def disconnect(self) -> None:
        self.slots.clear()

    def emit(self, *args) -> None:
        for slot in list(self.slots):
            slot(*args)


class _FakeWorker:
    """替代 TranslateWorker：记录文本，不起线程。"""

    def __init__(self, backend, config, text, parent=None) -> None:
        self.text = text
        self.token = _Sig()
        self.succeeded = _Sig()
        self.failed = _Sig()
        self.retry = _Sig()
        self.finished = _Sig()

    def start(self) -> None:
        pass

    def request_stop(self) -> None:
        pass

    def wait(self, _timeout_ms: int) -> bool:
        return True

    def deleteLater(self) -> None:
        pass


class _FakeWindow:
    """替代 MonitorWindow：记录调度侧的调用。"""

    def __init__(self) -> None:
        self.began: list[str] = []
        self.results: list[str] = []
        self.errors: list[str] = []
        self.status: list[str] = []
        self.closed = _Sig()

    def begin_translation(self, source: str) -> None:
        self.began.append(source)

    def append_token(self, _token: str) -> None:
        pass

    def set_translation(self, result: str) -> None:
        self.results.append(result)

    def show_error(self, message: str) -> None:
        self.errors.append(message)

    def show_status(self, text: str) -> None:
        self.status.append(text)

    def close(self) -> None:
        pass

    def deleteLater(self) -> None:
        pass


@pytest.fixture
def session(monkeypatch):
    """监控会话中的编排器：假小窗 + 假 worker 工厂，队列容量 2。"""
    workers: list[_FakeWorker] = []

    def factory(backend, config, text, parent=None) -> _FakeWorker:
        w = _FakeWorker(backend, config, text, parent)
        workers.append(w)
        return w

    monkeypatch.setattr(mt, "TranslateWorker", factory)
    t = MonitorTranslator({"backend": {"base_url": "http://127.0.0.1:9/v1"}})
    t._window = _FakeWindow()
    t._queue = deque()
    t._queue_max = 2
    t._dropped = 0
    return t, workers


def test_idle_new_text_starts_immediately(session):
    """空闲时新字幕立即翻译，不进队列。"""
    t, workers = session
    t._on_new_text("a")
    assert [w.text for w in workers] == ["a"]
    assert t._window.began == ["a"]
    assert list(t._queue) == []
    assert t._window.status[-1] == "翻译中"


def test_busy_new_text_enqueues(session):
    """在途翻译不被打断：新字幕排队，状态栏显示等待数。"""
    t, workers = session
    t._on_new_text("a")
    t._on_new_text("b")
    assert [w.text for w in workers] == ["a"]
    assert list(t._queue) == ["b"]
    assert t._window.status[-1] == "翻译中（1 条等待）"


def test_queue_full_drops_oldest(session):
    """队列满时丢弃最旧的等待项（保新），丢弃计数累计并提示。"""
    t, workers = session
    for ch in "abcd":
        t._on_new_text(ch)
    # a 在途，队列容量 2：b 被丢，c/d 等待
    assert [w.text for w in workers] == ["a"]
    assert list(t._queue) == ["c", "d"]
    assert t._dropped == 1
    assert "已丢弃 1 条" in t._window.status[-1]


def test_success_consumes_queue_in_order(session):
    """翻译完成后按 FIFO 续翻队首；全部完成前不丢句。"""
    t, workers = session
    t._on_new_text("a")
    t._on_new_text("b")
    t._on_translate_success("译文A")
    assert t._window.results == ["译文A"]
    assert t._last_text == "a"
    assert [w.text for w in workers] == ["a", "b"]
    assert list(t._queue) == []


def test_full_queue_drains_completely(session):
    """快对话突发（在途 + 排队）：逐条完成，顺序与内容都不丢。"""
    t, workers = session
    for ch in "abcd":
        t._on_new_text(ch)  # b 被丢（队列容量 2）
    t._on_translate_success("A")
    t._on_translate_success("C")
    assert [w.text for w in workers] == ["a", "c", "d"]
    assert t._window.results == ["A", "C"]


def test_failed_does_not_block_queue(session):
    """单条失败不阻塞队列：展示错误后继续翻下一条。"""
    t, workers = session
    t._on_new_text("a")
    t._on_new_text("b")
    t._on_translate_failed("boom")
    assert t._window.errors == ["boom"]
    assert [w.text for w in workers] == ["a", "b"]


def test_finished_restores_idle_status(session):
    """队列耗尽且线程退出后，状态回到「监控中」。"""
    t, workers = session
    t._on_new_text("a")
    t._on_translate_success("译A")
    # 模拟 QThread 收尾：forget_worker（track_worker 连接）先清引用，
    # 随后 _update_status 才能看到空闲
    workers[0].finished.emit()
    assert t._worker is None
    assert t._window.status[-1] == "监控中"


def test_window_closed_ignores_new_text(session):
    """小窗已关闭（会话停止在途）：新字幕不再触发翻译。"""
    t, workers = session
    t._window = None
    t._on_new_text("a")
    assert workers == []
    assert list(t._queue) == []


def test_stop_session_clears_queue(session):
    """停止会话：排队字幕作废，worker 取消，引用清空。"""
    t, workers = session
    t._on_new_text("a")
    t._on_new_text("b")
    t.stop_session()
    assert list(t._queue) == []
    assert t._worker is None
    assert t._window is None
