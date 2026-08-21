"""TranslateWorker 行为测试。

直接同步调用 worker.run()（不启动线程、不依赖 Qt 事件循环），
验证停止语义与信号发射：取消后不得再发请求、不得再 emit 结果信号。
另含监控鼠标门控的坐标判断（纯函数部分）。
"""

from __future__ import annotations

from app.core.backend import BackendError
from app.ui.worker import TranslateWorker, _cursor_in_bbox


class _StubBackend:
    """鸭子类型后端：记录调用，可选前 N 次抛瞬态错误。

    提供 interruptible_sleep（恒返回 False 不等待），
    使重试退避在测试中即时通过。
    """

    def __init__(self, fail_times: int = 0):
        self.calls = 0
        self.cancel_calls = 0
        self.fail_times = fail_times

    def chat_stream(self, messages, temperature=0.2, max_tokens=2048, on_token=None):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise BackendError("模拟瞬态失败", status_code=500)
        if on_token:
            on_token("译文")

    def interruptible_sleep(self, seconds: float) -> bool:
        return False

    def cancel(self) -> None:
        self.cancel_calls += 1


def _collect(worker: TranslateWorker) -> list:
    """接好全部信号并同步执行 run()，返回事件序列。"""
    events: list = []
    worker.token.connect(lambda t: events.append(("token", t)))
    worker.retry.connect(lambda: events.append(("retry",)))
    worker.succeeded.connect(lambda r: events.append(("succeeded", r)))
    worker.failed.connect(lambda m: events.append(("failed", m)))
    worker.run()
    return events


def test_worker_success_emits_tokens_and_result():
    backend = _StubBackend()
    w = TranslateWorker(backend, {"backend": {}}, "hello")
    events = _collect(w)
    assert ("token", "译文") in events
    assert ("succeeded", "译文") in events
    assert not any(name == "failed" for name, *_ in events)


def test_worker_stop_before_run_makes_no_request():
    """run 之前已取消：should_stop 拦截，不发起请求也不 emit 任何信号。"""
    backend = _StubBackend()
    w = TranslateWorker(backend, {"backend": {}}, "hello")
    w.request_stop()
    events = _collect(w)
    assert backend.calls == 0
    assert backend.cancel_calls == 1
    assert events == []


def test_worker_stop_mid_stream_suppresses_result():
    """流式输出期间取消：不 emit succeeded/failed。"""
    backend = _StubBackend()
    w = TranslateWorker(backend, {"backend": {}}, "hello")

    events: list = []
    w.token.connect(lambda _t: w.request_stop())  # 首个 token 到达即取消
    w.succeeded.connect(lambda r: events.append(("succeeded", r)))
    w.failed.connect(lambda m: events.append(("failed", m)))
    w.run()
    assert events == []


def test_worker_retry_emits_retry_then_succeeds():
    """瞬态失败触发重试：先 emit retry，最终 succeeded。"""
    backend = _StubBackend(fail_times=1)
    w = TranslateWorker(backend, {"backend": {}}, "hello")
    events = _collect(w)
    assert ("retry",) in events
    assert ("succeeded", "译文") in events
    assert backend.calls == 2


def test_worker_failure_emits_failed():
    """永久错误（4xx）不重试，直接 failed。"""
    backend = _StubBackend()

    def always_fail(messages, temperature=0.2, max_tokens=2048, on_token=None):
        backend.calls += 1
        raise BackendError("模型不存在", status_code=404)

    backend.chat_stream = always_fail
    w = TranslateWorker(backend, {"backend": {}}, "hello")
    events = _collect(w)
    assert backend.calls == 1  # 4xx 不重试
    assert len(events) == 1
    name, msg = events[0]
    assert name == "failed"
    assert "模型不存在" in msg


# ------------------------------------------------------------------ #
#  监控鼠标门控：_cursor_in_bbox（半开区间，物理像素）
# ------------------------------------------------------------------ #
_BBOX = (100, 200, 300, 260)


def test_cursor_inside_bbox():
    assert _cursor_in_bbox((100, 200), _BBOX)  # 左上角（含）
    assert _cursor_in_bbox((299, 259), _BBOX)  # 右下角内侧


def test_cursor_on_open_edges_excluded():
    """右/下边界为开区间：恰好在边上视为在区域外。"""
    assert not _cursor_in_bbox((300, 230), _BBOX)
    assert not _cursor_in_bbox((200, 260), _BBOX)


def test_cursor_outside_bbox():
    assert not _cursor_in_bbox((99, 200), _BBOX)  # 左侧外
    assert not _cursor_in_bbox((100, 199), _BBOX)  # 上方外
    assert not _cursor_in_bbox((1000, 1000), _BBOX)  # 远处
