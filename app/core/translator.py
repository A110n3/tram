"""翻译编排。

流程：分段 -> 逐块构造带上下文/术语表的提示词 -> 流式调用后端。
支持 on_token 增量回调，供 GUI 流式显示。
单块翻译失败自动重试，但对永久性错误（4xx）立即失败。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

from ..config import get_default
from .backend import BackendError, OpenAIBackend, StreamCancelled
from .chunking import split_text
from .glossary import to_prompt_block
from .prompts import build_messages

logger = logging.getLogger(__name__)

# 4xx 状态码视为永久错误，不重试；5xx 视为瞬态错误，可重试
_PERMANENT_RANGE = (400, 500)  # [400, 500)
_MAX_RETRIES = 3
_RETRY_BASE_DELAY = 1.5  # 退避基数：attempt × 1.5s（1.5s / 3.0s）


def _is_retryable(err: Exception) -> bool:
    """判断错误是否值得重试（瞬态错误）。"""
    if isinstance(err, BackendError) and err.status_code is not None:
        lo, hi = _PERMANENT_RANGE
        return not (lo <= err.status_code < hi)
    # 网络超时等无状态码错误可重试
    return True


def _backoff_sleep(backend: object, seconds: float) -> bool:
    """退避等待，返回期间是否被取消。

    OpenAIBackend 提供 interruptible_sleep（Event.wait，cancel 立即唤醒）；
    测试替身等鸭子类型对象回退到 time.sleep，取消与否交给调用方的
    should_stop 检查兜底。
    """
    sleeper = getattr(backend, "interruptible_sleep", None)
    if callable(sleeper):
        return bool(sleeper(seconds))
    time.sleep(seconds)
    return False


class Translator:
    def __init__(
        self,
        backend: OpenAIBackend,
        config: dict,
        should_stop: Callable[[], bool] | None = None,
    ):
        self.backend = backend
        self.config = config
        # 外部停止检查（如 TranslateWorker 的 _stop_flag）：
        # 覆盖 cancel() 只能中断"正在进行的请求"、无法中断"退避 sleep /
        # 下一块尚未开始"的窗口期，防止已取消的任务重新发起请求。
        self._should_stop = should_stop

    def _stopped(self) -> bool:
        return self._should_stop is not None and self._should_stop()

    def translate(
        self,
        text: str,
        on_token: Callable[[str], None] | None = None,
        on_retry: Callable[[], None] | None = None,
    ) -> str:
        """翻译整段文本，返回完整译文。

        on_token: 每个增量 token 回调（含换行）。
        on_retry: 单块翻译重试前回调，用于通知 UI 回滚该块已显示的内容。
        """
        tcfg = self.config.get("translation", {})
        source_lang = tcfg.get("source_lang", get_default("translation", "source_lang"))
        target_lang = tcfg.get("target_lang", get_default("translation", "target_lang"))
        style = tcfg.get("style", get_default("translation", "style"))
        chunk_chars = tcfg.get("chunk_chars", get_default("translation", "chunk_chars"))
        # 不支持 system 角色的后端：系统提示词并入 user 消息
        use_system_role = self.config.get("backend", {}).get(
            "use_system_role", get_default("backend", "use_system_role")
        )

        glossary_block = to_prompt_block(self.config.get("glossary", []))
        custom_prompt = tcfg.get("custom_prompt", get_default("translation", "custom_prompt"))
        chunks = split_text(text, chunk_chars)

        full_result: list[str] = []
        prev_result: str | None = None  # 前一块译文，用于保持术语/风格一致

        for chunk in chunks:
            # 每块开始前检查取消：多块翻译被取消时不再发起后续块请求
            if self._stopped():
                raise StreamCancelled()

            context_block = ""
            if prev_result:
                # 块头使用英文：部分后端无法处理请求中的非 ASCII 字符
                context_block = (
                    "Previously translated content for reference (keep terms,"
                    " tone and style consistent; do not retranslate it):\n"
                    f"{prev_result}"
                )

            messages = build_messages(
                chunk,
                target_lang=target_lang,
                source_lang=source_lang,
                style=style,
                glossary_block=glossary_block,
                context_block=context_block,
                merge_system=not use_system_role,
                custom_prompt=custom_prompt,
            )

            result = self._translate_chunk(
                messages, on_token=on_token, on_retry=on_retry
            )
            full_result.append(result)
            prev_result = result

        return "\n\n".join(full_result)

    def _translate_chunk(
        self,
        messages: list[dict],
        on_token: Callable[[str], None] | None = None,
        on_retry: Callable[[], None] | None = None,
    ) -> str:
        """翻译单块，失败自动重试（最多 3 次，指数退避）。

        永久性错误（4xx）立即失败，不重试。
        注意：StreamCancelled 非 BackendError 子类，不会被此处捕获，
        直接穿透到调用方——确保取消时不触发重试、不继续后续块。
        """
        bcfg = self.config.get("backend", {})
        temperature = bcfg.get("temperature", get_default("backend", "temperature"))
        max_tokens = bcfg.get("max_tokens", get_default("backend", "max_tokens"))

        last_err: Exception | None = None
        collected: list[str] = []

        def _on_token(t: str) -> None:
            collected.append(t)
            if on_token:
                on_token(t)

        for attempt in range(_MAX_RETRIES):
            # 重试前检查取消：cancel() 无法中断退避 sleep，若不拦截，
            # 已取消的任务会在 sleep 结束后重新发起完整请求
            if self._stopped():
                raise StreamCancelled()
            if attempt > 0:
                if on_retry:
                    on_retry()
                collected.clear()
                delay = _RETRY_BASE_DELAY * attempt  # 退避：1.5s / 3.0s
                logger.info("翻译重试 #%d，等待 %.1fs", attempt, delay)
                if _backoff_sleep(self.backend, delay):
                    raise StreamCancelled()  # 等待期间被 cancel 打断
                if self._stopped():
                    raise StreamCancelled()
            try:
                self.backend.chat_stream(
                    messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    on_token=_on_token,
                )
                result = "".join(collected).strip()
                if result:
                    return result
                # 流正常结束但无内容：思考型模型（reasoning_content）
                # 可能把 max_tokens 耗尽在思考阶段，content 为空。
                # 视为瞬态错误走重试，重试仍为空则最终报错，
                # 避免浮窗被静默清空、无任何提示。
                last_err = BackendError(
                    "模型返回空结果（思考过程可能耗尽 max_tokens，"
                    "可在设置中调大 max_tokens）"
                )
                logger.warning("翻译返回空结果 (attempt %d/%d)",
                               attempt + 1, _MAX_RETRIES)
            except BackendError as e:
                last_err = e
                if not _is_retryable(e):
                    logger.warning("翻译永久错误，不重试: %s", e)
                    raise
                logger.warning("翻译失败 (attempt %d/%d): %s",
                               attempt + 1, _MAX_RETRIES, e)
        raise last_err or BackendError("翻译失败")
