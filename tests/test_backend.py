"""后端客户端测试（用 respx mock HTTP）。"""

from __future__ import annotations

import httpx
import respx

from app.core.backend import BackendError, OpenAIBackend


@respx.mock
def test_chat_stream_parses_sse():
    """流式解析标准 SSE 事件。"""
    sse_lines = [
        'data: {"choices":[{"delta":{"content":"Hello"}}]}',
        'data: {"choices":[{"delta":{"content":" world"}}]}',
        'data: [DONE]',
    ]
    body = chr(10).join(sse_lines)
    respx.post('https://example.com/v1/chat/completions').mock(
        return_value=httpx.Response(200, text=body)
    )

    backend = OpenAIBackend('https://example.com/v1', 'key', 'test')
    tokens = []
    backend.chat_stream([], on_token=tokens.append)
    assert ''.join(tokens) == 'Hello world'
    backend.close()


@respx.mock
def test_chat_stream_error_status():
    """后端返回 4xx 时抛 BackendError 含状态码。"""
    respx.post('https://example.com/v1/chat/completions').mock(
        return_value=httpx.Response(404, text='model not found')
    )

    backend = OpenAIBackend('https://example.com/v1', 'key', 'test')
    try:
        backend.chat_stream([])
        assert False, '应抛异常'
    except BackendError as e:
        assert e.status_code == 404
    finally:
        backend.close()


@respx.mock
def test_chat_stream_connection_error():
    """连接失败时抛 BackendError。"""
    respx.post('https://example.com/v1/chat/completions').mock(
        side_effect=httpx.ConnectError('refused')
    )

    backend = OpenAIBackend('https://example.com/v1', 'key', 'test')
    try:
        backend.chat_stream([])
        assert False, '应抛异常'
    except BackendError as e:
        assert '无法连接' in str(e)
    finally:
        backend.close()


@respx.mock
def test_chat_stream_cancel():
    """cancel() 中断后，已设置的 _cancel_event 阻止后续 token。"""
    sse_lines = [
        'data: {"choices":[{"delta":{"content":"A"}}]}',
        'data: {"choices":[{"delta":{"content":"B"}}]}',
        'data: [DONE]',
    ]
    body = chr(10).join(sse_lines)
    respx.post('https://example.com/v1/chat/completions').mock(
        return_value=httpx.Response(200, text=body)
    )

    backend = OpenAIBackend('https://example.com/v1', 'key', 'test')
    tokens = []

    def on_token(t):
        tokens.append(t)
        if len(tokens) == 1:
            backend.cancel()

    backend.chat_stream([], on_token=on_token)
    # 取消后应只收到部分 token
    assert tokens == ['A']
    backend.close()
