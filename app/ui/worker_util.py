"""QThread worker 生命周期管理工具。

提供 track_worker / forget_worker 两个工具函数，统一处理 QThread
的引用清理与僵尸包装器免疫。MainWindow、SelectionTranslator、
SettingsDialog 共享同一套清理逻辑，避免重复。
"""

from __future__ import annotations

from PyQt6.QtCore import QThread


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
