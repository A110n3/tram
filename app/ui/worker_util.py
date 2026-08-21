"""QThread worker 生命周期管理工具。

提供 track_worker / forget_worker 两个工具函数，统一处理 QThread
的引用清理与僵尸包装器免疫。MainWindow、SelectionTranslator、
SettingsDialog 共享同一套清理逻辑，避免重复。
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Callable
from typing import Any

from PyQt6.QtCore import QThread

logger = logging.getLogger(__name__)


def track_worker(owner: object, attr: str, w: QThread) -> None:
    """注册 finished 信号自动清理：先清 Python 引用，再删 C++ 对象。

    引用必须及时清空，否则 deleteLater 删掉 C++ 对象后，
    残留的包装器成为"僵尸"，后续任何调用都会 RuntimeError。
    finished 信号可能连接了多个槽，track_worker 追加两个：
    forget_worker（清引用）和 deleteLater（删 C++ 对象）。
    """
    w.finished.connect(lambda: forget_worker(owner, attr, w))
    w.finished.connect(w.deleteLater)


def forget_worker(owner: object, attr: str, w: object) -> None:
    """线程结束后清引用；身份校验防止误清更新的 worker。"""
    if getattr(owner, attr, None) is w:
        setattr(owner, attr, None)


def launch_worker(
    owner: object,
    attr: str,
    # 实际类型为 _BackendRequestWorker（带 ok/err 信号）；PyQt 信号在
    # stub 中是描述符，精确标注收益低，此处按 Any 处理
    worker: Any,
    *,
    # ok 信号携带 object；回调形参多为具体类型（str 等），
    # 用 Any 避免 Callable 参数逆变导致的不兼容
    on_ok: Callable[[Any], None] | None = None,
    on_err: Callable[[str], None] | None = None,
    on_finished: Callable[[], None] | None = None,
) -> None:
    """启动 worker 的通用辅助：连接信号 → 注册清理 → start。

    统一 MainWindow 和 SettingsDialog 中的重复模式：
    创建 worker、连接 ok/err/finished 信号、track_worker、start。

    owner: 持有 worker 的对象（self）
    attr: worker 属性名（如 "_warmup_worker"）
    worker: 要启动的 _BackendRequestWorker 实例
    on_ok: ok 信号回调（可选）
    on_err: err 信号回调（可选）
    on_finished: finished 信号回调（可选）
    """
    if on_ok is not None:
        worker.ok.connect(on_ok)
    if on_err is not None:
        worker.err.connect(on_err)
    if on_finished is not None:
        worker.finished.connect(on_finished)
    track_worker(owner, attr, worker)
    worker.start()


def drop_worker(owner: object, attr: str, disconnect_results: bool = True) -> None:
    """作废 worker：断开结果信号，线程自行结束并清理。

    owner: 持有 worker 的对象（self）
    attr: worker 属性名（如 "_warmup_worker"）
    disconnect_results: 是否断开 ok/err 信号（默认 True）

    适用于快速连续触发时只认最新一次结果的场景
    （如连续切换语言，只认最后一次测试结果）。
    """
    w = getattr(owner, attr, None)
    if w is None:
        return
    if disconnect_results:
        with contextlib.suppress(TypeError, RuntimeError):
            w.ok.disconnect()
        with contextlib.suppress(TypeError, RuntimeError):
            w.err.disconnect()
    setattr(owner, attr, None)


def shutdown_worker(
    owner: object,
    attr: str,
    name: str = "worker",
    timeout_ms: int = 3000,
) -> None:
    """安全关闭 worker：请求停止 → 等待退出 → 断开信号 → deleteLater。

    owner: 持有 worker 的对象（self）
    attr: worker 属性名（如 "_test_worker"）
    name: worker 名称（用于日志）
    timeout_ms: 等待退出超时（毫秒，默认 3s）

    对"僵尸"包装器（C++ 对象已被 deleteLater 删除）免疫：
    任何 RuntimeError 直接静默跳过。

    线程超时未退出时：仅断开结果信号（ok/err），保留 finished
    信号不动，让线程自然结束后由 finished -> deleteLater 清理。
    避免对运行中的 QThread 调用 deleteLater 导致崩溃。
    """
    w = getattr(owner, attr, None)
    setattr(owner, attr, None)
    if w is None:
        return
    try:
        # 先请求取消（中断阻塞中的 HTTP 请求），缩短等待时间
        if hasattr(w, "request_stop"):
            w.request_stop()
        if w.isRunning() and not w.wait(timeout_ms):
            logger.warning("%s线程 %dms 未退出，放弃等待", name, timeout_ms)
            # 线程仍在运行：仅断开结果信号，不 deleteLater
            # finished 信号保留，线程结束后自动 forget_worker + deleteLater
            with contextlib.suppress(TypeError, RuntimeError):
                w.ok.disconnect()
                w.err.disconnect()
            return
    except RuntimeError:
        return  # C++ 对象已删除，无需清理
    # 线程已结束：安全断开所有信号并调度删除
    with contextlib.suppress(TypeError, RuntimeError):
        w.ok.disconnect()
        w.err.disconnect()
        w.finished.disconnect()
    # deleteLater 由 finished 信号触发；若线程已结束则立即调度
    with contextlib.suppress(RuntimeError):
        w.deleteLater()
