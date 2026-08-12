"""OpenAI 兼容后端客户端。

任何提供 /v1/chat/completions 的本地推理服务都可以接入：
- Ollama:       http://localhost:11434/v1
- LM Studio:    http://localhost:1234/v1
- vLLM / 其他:  自定义 base_url

统一接口：chat_stream()（流式）与 chat()（非流式）。
支持通过 close() 中断进行中的流式请求。
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable

import httpx

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 180


class BackendError(Exception):
    """后端连接或响应异常。"""

    def __init__(self, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class OpenAIBackend:
    def __init__(
        self,
        base_url: str,
        api_key: str = "ollama",
        model: str = "",
        timeout: int = DEFAULT_TIMEOUT,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key or "ollama"
        self.model = model
        self.timeout = timeout
        self._client = httpx.Client(
            base_url=self.base_url, timeout=timeout
        )
        self._cancel_event = threading.Event()

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def chat(
        self,
        messages: list[dict],
        temperature: float = 0.2,
        max_tokens: int = 2048,
    ) -> str:
        """非流式对话，返回完整回复文本。"""
        chunks: list[str] = []
        self.chat_stream(
            messages,
            temperature=temperature,
            max_tokens=max_tokens,
            on_token=lambda t: chunks.append(t),
        )
        return "".join(chunks)

    def chat_stream(
        self,
        messages: list[dict],
        temperature: float = 0.2,
        max_tokens: int = 2048,
        on_token: Callable[[str], None] | None = None,
    ) -> None:
        """流式对话。on_token 逐个接收增量文本。

        SSE 事件格式（OpenAI 兼容）：
            data: {"choices":[{"delta":{"content":"..."}}]}
            data: [DONE]
        """
        self._cancel_event.clear()
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
        }
        try:
            with self._client.stream(
                "POST", "/chat/completions", json=payload, headers=self._headers()
            ) as resp:
                if resp.status_code >= 400:
                    body = resp.read().decode("utf-8", errors="replace")[:500]
                    raise BackendError(
                        f"后端返回 {resp.status_code}: {body}",
                        status_code=resp.status_code,
                    )
                for line in resp.iter_lines():
                    # 取消检查：close() 设置事件后立即中断
                    if self._cancel_event.is_set():
                        logger.debug("流式请求被取消")
                        return
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[len("data:"):].strip()
                    if data == "[DONE]":
                        break
                    try:
                        obj = json.loads(data)
                        delta = obj["choices"][0].get("delta", {})
                        token = delta.get("content", "")
                    except (json.JSONDecodeError, KeyError, IndexError):
                        continue
                    if token and on_token:
                        on_token(token)
        except httpx.HTTPError as e:
            if self._cancel_event.is_set():
                return  # 取消导致的网络错误，静默
            raise BackendError(f"无法连接后端 {self.base_url}: {e}") from e

    def cancel(self) -> None:
        """中断进行中的流式请求。下一次 chat_stream 前会自动清除。"""
        self._cancel_event.set()

    def close(self) -> None:
        """关闭底层连接，中断进行中的请求。"""
        self._cancel_event.set()
        try:
            self._client.close()
        except Exception:
            logger.debug("关闭 httpx client 时异常", exc_info=True)


def test_connection(base_url: str, api_key: str, model: str) -> str:
    """发送最小请求，验证后端可用。返回后端实际回应文本。"""
    backend = OpenAIBackend(base_url, api_key, model, timeout=30)
    try:
        return backend.chat(
            [{"role": "user", "content": "Hi"}],
            temperature=0.0,
            max_tokens=8,
        )
    finally:
        backend.close()
