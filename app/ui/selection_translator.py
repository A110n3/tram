"""划词翻译编排器。

连接热键监听、取词、翻译后端和悬浮窗，管理整个划词翻译生命周期。
与 OCR 翻译使用独立 backend 实例，互不干扰。

公共骨架（后端/热键生命周期、去重缓存、翻译管线）见 base_translator。
"""

from __future__ import annotations

import logging

from ..config import get_default
from ..core.selection import grab_selection
from .base_translator import BaseHotkeyTranslator

logger = logging.getLogger(__name__)


class SelectionTranslator(BaseHotkeyTranslator):
    """管理划词翻译全流程：热键 -> 取词 -> 翻译 -> 悬浮窗。"""

    section = "selection"
    service_name = "划词"
    hotkey_id = 1

    # ---------- 热键处理 ----------
    def _on_hotkey(self) -> None:
        # 1. 取消进行中的翻译；旧线程未死透则拒绝本轮：
        #    backend 为共享实例，并发请求的连接与取消事件会互相干扰
        if not self._cancel_workers():
            if self._popup:
                self._popup.show_error(
                    "上一次翻译仍在结束中，请稍后重试", can_retry=False
                )
            return
        if self._popup:
            self._popup.hide()
            self._popup = None

        sel_cfg = self._config.get("selection", {})
        # 先以极简窗告知"正在捕获"，定位在鼠标旁
        popup = self._new_popup()
        popup.show_capturing()

        # 2. 取词（可能阻塞最多 400ms，但用户已看到反馈）
        text = grab_selection()
        if not text or not text.strip():
            popup.fade_out()
            return

        stripped = text.strip()
        min_chars = sel_cfg.get("min_chars", get_default("selection", "min_chars"))
        if len(stripped) < min_chars:
            popup.hide()
            return
        if self._try_show_cached(stripped):
            return
        # 3. 取词成功，进入公共翻译管线
        self._begin_translation(stripped)
