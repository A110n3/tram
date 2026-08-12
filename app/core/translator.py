"""翻译编排。

流程：分段 -> 逐块构造带上下文/术语表的提示词 -> 流式调用后端。
支持 on_token（增量文本）与 on_chunk 回调，供 GUI 流式显示。
"""

from __future__ import annotations

from typing import Callable, Optional

from .backend import BackendError, OpenAIBackend
from .chunking import split_text
from .glossary import to_prompt_block
from .prompts import build_messages


class Translator:
    def __init__(self, backend: OpenAIBackend, config: dict):
        self.backend = backend
        self.config = config

    def translate(
        self,
        text: str,
        on_token: Optional[Callable[[str], None]] = None,
        on_chunk: Optional[Callable[[int, int], None]] = None,
    ) -> str:
        """翻译整段文本，返回完整译文。

        on_token: 每个增量 token 回调（含换行）。
        on_chunk: 每个块开始/结束回调 (index, total)，可用于进度显示。
        """
        tcfg = self.config.get("translation", {})
        target_lang = tcfg.get("target_lang", "中文（简体）")
        style = tcfg.get("style", "忠实原文")
        chunk_chars = tcfg.get("chunk_chars", 2000)

        glossary_block = to_prompt_block(
            self.config.get("glossary", [])
        )
        chunks = split_text(text, chunk_chars)
        total = len(chunks)

        full_result: list[str] = []
        prev_chunk: Optional[str] = None  # 前一块原文，用于上下文

        for index, chunk in enumerate(chunks):
            if on_chunk:
                on_chunk(index, total)

            context_block = ""
            if prev_chunk is not None:
                context_block = (
                    "前文已翻译的内容参考（保持术语、语气、风格一致，无需重复翻译）：\n"
                    f"{prev_chunk}"
                )

            messages = build_messages(
                chunk,
                target_lang=target_lang,
                style=style,
                glossary_block=glossary_block,
                context_block=context_block,
            )

            result = self._translate_chunk(
                messages, on_token=on_token
            )
            full_result.append(result)
            prev_chunk = chunk

        return "\n".join(full_result)

    def _translate_chunk(self, messages: list[dict], on_token) -> str:
        """翻译单块，失败自动重试（最多 3 次，指数退避）。"""
        bcfg = self.config.get("backend", {})
        temperature = bcfg.get("temperature", 0.2)
        max_tokens = bcfg.get("max_tokens", 2048)

        last_err: Optional[Exception] = None
        collected: list[str] = []

        def _on_token(t: str) -> None:
            collected.append(t)
            if on_token:
                on_token(t)

        for attempt in range(3):
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
                if attempt < 2:
                    import time
                    time.sleep(1.5 * (attempt + 1))  # 简单退避
        raise last_err or BackendError("翻译失败")
