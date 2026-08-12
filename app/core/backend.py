"""OpenAI 兼容后端客户端。

任何提供 /v1/chat/completions 的本地推理服务都可以接入：
- Ollama:       http://localhost:11434/v1
- LM Studio:    http://localhost:1234/v1
- vLLM / 其他:  自定义 base_url

统一接口：chat_stream()（流式）与 chat()（非流式）。
"""

from __future__ import annotations

import json
from typing import Callable, Optional

import httpx

DEFAULT_TIMEOUT = 180


class BackendError(Exception):
    """后端连接或响应异常。"""


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
        self._client = httpx.Client(base_url=self.base_url, timeout=timeout)

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
            messages, temperature=temperature, max_tokens=max_tokens,
            on_token=lambda t: chunks.append(t),
        )
        return "".join(chunks)

    def chat_stream(
        self,
        messages: list[dict],
        temperature: float = 0.2,
        max_tokens: int = 2048,
        on_token: Optional[Callable[[str], None]] = None,
    ) -> None:
        """流式对话。on_token 逐个接收增量文本。

        SSE 事件格式（OpenAI 兼容）：
            data: {"choices":[{"delta":{"content":"..."}}]}
            data: [DONE]
        """
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
                        f"后端返回 {resp.status_code}: {body}"
                    )
                for line in resp.iter_lines():
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
            raise BackendError(f"无法连接后端 {self.base_url}: {e}") from e

    def close(self) -> None:
        self._client.close()


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
