"""翻译编排。

流程：分段 -> 逐块构造带上下文/术语表的提示词 -> 流式调用后端。
支持 on_token（增量文本）与 on_chunk 回调，供 GUI 流式显示。
单块翻译失败自动重试，但对永久性错误（4xx）立即失败。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

from .backend import BackendError, OpenAIBackend
from .chunking import split_text
from .glossary import to_prompt_block
from .prompts import build_messages

logger = logging.getLogger(__name__)

# 4xx 状态码视为永久错误，不重试；5xx 视为瞬态错误，可重试
_PERMANENT_RANGE = (400, 500)  # [400, 500)
_MAX_RETRIES = 3


def _is_retryable(err: Exception) -> bool:
    """判断错误是否值得重试（瞬态错误）。"""
    if isinstance(err, BackendError) and err.status_code is not None:
        lo, hi = _PERMANENT_RANGE
        return not (lo <= err.status_code < hi)
    # 网络超时等无状态码错误可重试
    return True


class Translator:
    def __init__(self, backend: OpenAIBackend, config: dict):
        self.backend = backend
        self.config = config

    def translate(
        self,
        text: str,
        on_token: Callable[[str], None] | None = None,
        on_chunk: Callable[[int, int], None] | None = None,
        on_retry: Callable[[], None] | None = None,
    ) -> str:
        """翻译整段文本，返回完整译文。

        on_token: 每个增量 token 回调（含换行）。
        on_chunk: 每个块开始/结束回调 (index, total)，可用于进度显示。
        on_retry: 单块翻译重试前回调，用于通知 UI 回滚该块已显示的内容。
        """
        tcfg = self.config.get("translation", {})
        source_lang = tcfg.get("source_lang", "自动识别")
        target_lang = tcfg.get("target_lang", "中文（简体）")
        style = tcfg.get("style", "忠实原文")
        chunk_chars = tcfg.get("chunk_chars", 2000)
        # 不支持 system 角色的后端：系统提示词并入 user 消息
        use_system_role = self.config.get("backend", {}).get(
            "use_system_role", True
        )

        glossary_block = to_prompt_block(self.config.get("glossary", []))
        chunks = split_text(text, chunk_chars)
        total = len(chunks)

        full_result: list[str] = []
        prev_chunk: str | None = None  # 前一块原文，用于上下文

        for index, chunk in enumerate(chunks):
            if on_chunk:
                on_chunk(index, total)

            context_block = ""
            if prev_chunk is not None:
                # 块头使用英文：部分后端无法处理请求中的非 ASCII 字符
                context_block = (
                    "Previously translated content for reference (keep terms,"
                    " tone and style consistent; do not retranslate it):\n"
                    f"{prev_chunk}"
                )

            messages = build_messages(
                chunk,
                target_lang=target_lang,
                source_lang=source_lang,
                style=style,
                glossary_block=glossary_block,
                context_block=context_block,
                merge_system=not use_system_role,
            )

            result = self._translate_chunk(
                messages, on_token=on_token, on_retry=on_retry
            )
            full_result.append(result)
            prev_chunk = chunk

        return "\n".join(full_result)

    def _translate_chunk(
        self,
        messages: list[dict],
        on_token,
        on_retry: Callable[[], None] | None = None,
    ) -> str:
        """翻译单块，失败自动重试（最多 3 次，指数退避）。

        永久性错误（4xx）立即失败，不重试。
        """
        bcfg = self.config.get("backend", {})
        temperature = bcfg.get("temperature", 0.2)
        max_tokens = bcfg.get("max_tokens", 2048)

        last_err: Exception | None = None
        collected: list[str] = []

        def _on_token(t: str) -> None:
            collected.append(t)
            if on_token:
                on_token(t)

        for attempt in range(_MAX_RETRIES):
            if attempt > 0:
                if on_retry:
                    on_retry()
                collected.clear()
                delay = 1.5 * attempt  # 退避：1.5s / 3.0s
                logger.info("翻译重试 #%d，等待 %.1fs", attempt, delay)
                time.sleep(delay)
            try:
                self.backend.chat_stream(
                    messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    on_token=_on_token,
                )
                return "".join(collected).strip()
            except BackendError as e:
                last_err = e
                if not _is_retryable(e):
                    logger.warning("翻译永久错误，不重试: %s", e)
                    raise
                logger.warning("翻译失败 (attempt %d/%d): %s",
                               attempt + 1, _MAX_RETRIES, e)
        raise last_err or BackendError("翻译失败")
