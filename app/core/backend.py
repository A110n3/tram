"""OpenAI 兼容后端客户端。

任何提供 /v1/chat/completions 的本地推理服务都可以接入：
- Ollama:       http://localhost:11434/v1
- LM Studio:    http://localhost:1234/v1
- vLLM / 其他:  自定义 base_url

统一接口：chat_stream()（流式）与 chat()（非流式）。
支持通过 cancel() 中断进行中的流式请求，同时保持连接池复用。
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable

import httpx

from .prompts import build_messages

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 180


class BackendError(Exception):
    """后端连接或响应异常。"""

    def __init__(self, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class StreamCancelled(Exception):
    """流式请求被主动取消（cancel/close）。

    与 BackendError 区分：取消不应触发重试，需要立刻穿透分块循环
    终止整个翻译任务。
    """

    def __init__(self) -> None:
        super().__init__("翻译请求被取消")


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
        # trust_env=False：禁止读取系统代理（环境变量 + Windows 注册表）。
        # 本地推理后端必须直连；若走系统代理（如 Clash），发往
        # localhost 的请求会被代理拒绝并返回 502 空响应体。
        self._client = httpx.Client(
            base_url=self.base_url, timeout=timeout, trust_env=False
        )
        self._cancel_event = threading.Event()
        # 当前活跃的响应对象，用于细粒度取消（只关闭当前请求的流）
        self._current_response: httpx.Response | None = None
        self._response_lock = threading.Lock()

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def _ensure_client(self) -> httpx.Client:
        """返回可用的 httpx 客户端；被 close 关闭后自动重建。

        注意：cancel() 现在只关闭当前请求，不关闭整个 client，
        因此连接池可以在多次请求间复用，提升性能。
        """
        if self._client.is_closed:
            self._client = httpx.Client(
                base_url=self.base_url, timeout=self.timeout, trust_env=False
            )
        return self._client

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
            # 在 stream() 外部就获取响应对象，这样 cancel() 可以打断连接建立阶段
            req = self._ensure_client().build_request(
                "POST", "/chat/completions", json=payload, headers=self._headers()
            )
            resp = self._ensure_client().send(req, stream=True)

            # 记录当前响应对象，供 cancel() 关闭
            with self._response_lock:
                self._current_response = resp

            try:
                if resp.status_code >= 400:
                    body = resp.read().decode("utf-8", errors="replace")
                    body = body.strip()[:500] or "（无响应体）"
                    raise BackendError(
                        f"后端返回 {resp.status_code}: {body}",
                        status_code=resp.status_code,
                    )
                for line in resp.iter_lines():
                    # 取消检查：cancel() 设置事件后立即中断
                    if self._cancel_event.is_set():
                        logger.debug("流式请求被取消")
                        raise StreamCancelled()
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
            finally:
                # 清除当前响应引用并关闭响应
                with self._response_lock:
                    self._current_response = None
                try:
                    resp.close()
                except Exception:
                    pass
        except httpx.HTTPError as e:
            if self._cancel_event.is_set():
                raise StreamCancelled() from e  # 取消导致的网络错误
            raise BackendError(f"无法连接后端 {self.base_url}: {e}") from e

    def list_models(self) -> list[str]:
        """获取后端可用模型列表（OpenAI 兼容 GET /models）。

        响应格式：{"object": "list", "data": [{"id": "...", ...}, ...]}
        返回去重后的模型 id 列表（保持后端返回顺序）。
        """
        try:
            resp = self._ensure_client().get("/models", headers=self._headers())
        except httpx.HTTPError as e:
            raise BackendError(f"无法连接后端 {self.base_url}: {e}") from e
        if resp.status_code >= 400:
            body = resp.text.strip()[:500] or "（无响应体）"
            raise BackendError(
                f"获取模型列表失败，后端返回 {resp.status_code}: {body}",
                status_code=resp.status_code,
            )
        try:
            obj = resp.json()
        except ValueError as e:  # json.JSONDecodeError 是 ValueError 子类
            raise BackendError("获取模型列表失败：响应不是有效 JSON") from e
        items = obj.get("data") if isinstance(obj, dict) else None
        if not isinstance(items, list):
            raise BackendError(
                "获取模型列表失败：响应缺少 data 列表"
                "（后端未实现 OpenAI 兼容 /models 接口）"
            )
        models: list[str] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            mid = item.get("id")
            if isinstance(mid, str) and mid.strip() and mid not in models:
                models.append(mid)
        return models

    def interruptible_sleep(self, seconds: float) -> bool:
        """退避等待：被 cancel() 提前打断时返回 True。

        cancel() 会设置 _cancel_event，Event.wait 随即返回，
        正在退避重试的翻译线程无需睡满整个延迟即可退出。
        """
        return self._cancel_event.wait(seconds)

    def cancel(self) -> None:
        """中断进行中的流式请求，同时保持连接池可复用。

        采用两步策略：
        1. 设置取消事件：通知正在读取响应的线程退出
        2. 关闭响应对象（若已创建）：打断阻塞的读取
        3. 关闭 client：强制中断阻塞在连接建立或首字节等待的请求

        关闭 client 后，下次请求会自动重建连接池。
        """
        self._cancel_event.set()

        with self._response_lock:
            resp = self._current_response

        if resp is not None:
            try:
                resp.close()
            except Exception:
                logger.debug("关闭响应流时异常", exc_info=True)

        # 始终关闭 client，确保阻塞在连接建立或首字节等待的请求被打断
        try:
            self._client.close()
        except Exception:
            logger.debug("关闭 client 时异常", exc_info=True)

    def close(self) -> None:
        """关闭底层连接池，释放所有资源。

        应用退出时调用，或需要完全清理后端对象时调用。
        日常的取消操作应使用 cancel()，它不会破坏连接池。
        """
        self._cancel_event.set()
        with self._response_lock:
            if self._current_response is not None:
                try:
                    self._current_response.close()
                except Exception:
                    logger.debug("关闭响应流时异常", exc_info=True)
                self._current_response = None
        try:
            self._client.close()
        except Exception:
            logger.debug("关闭 httpx client 时异常", exc_info=True)

    # context manager 支持，简化 try/finally/close 模式
    def __enter__(self) -> OpenAIBackend:
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()


def build_test_messages(use_system_role: bool = True) -> list[dict]:
    """构造与真实翻译结构一致的最小测试请求消息。

    使用 build_messages 构造（默认含 system 角色），以便在设置阶段
    就发现不支持 system 消息的后端（此类后端连接测试能通过简单请求、
    但真实翻译会返回 5xx）。供 test_connection 与 TestConnectionWorker
    共用，后者需要自持 backend 引用以支持取消。
    """
    return build_messages(
        "Hello",
        target_lang="中文（简体）",
        merge_system=not use_system_role,
    )


def test_connection(
    base_url: str, api_key: str, model: str, use_system_role: bool = True
) -> str:
    """发送最小测试请求，验证后端可用（消息结构见 build_test_messages）。"""
    messages = build_test_messages(use_system_role)
    with OpenAIBackend(base_url, api_key, model, timeout=30) as backend:
        return backend.chat(messages, temperature=0.0, max_tokens=8)


def fetch_models(base_url: str, api_key: str = "", timeout: int = 15) -> list[str]:
    """获取后端可用模型列表（OpenAI 兼容 GET /models）。"""
    with OpenAIBackend(base_url, api_key, timeout=timeout) as backend:
        return backend.list_models()
