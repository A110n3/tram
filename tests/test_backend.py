"""后端客户端测试（用 respx mock HTTP）。"""

from __future__ import annotations

import json

import httpx
import respx

# 别名导入：test_connection 以 test_ 开头，直接导入会被 pytest 误收集为用例
from app.core.backend import BackendError, OpenAIBackend
from app.core.backend import test_connection as run_connection_test


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
def test_chat_stream_empty_error_body():
    """5xx 且响应体为空时（如不支持 system 的后端），消息含提示。"""
    respx.post('https://example.com/v1/chat/completions').mock(
        return_value=httpx.Response(502, text='')
    )

    backend = OpenAIBackend('https://example.com/v1', 'key', 'test')
    try:
        backend.chat_stream([])
        assert False, '应抛异常'
    except BackendError as e:
        assert e.status_code == 502
        assert '无响应体' in str(e)
    finally:
        backend.close()


_SSE_OK = 'data: {"choices":[{"delta":{"content":"ok"}}]}\ndata: [DONE]'


def test_client_disables_system_proxy():
    """回归：必须禁用系统代理，否则本地后端请求会被代理拒绝返回 502。"""
    backend = OpenAIBackend('https://example.com/v1', 'key', 'test')
    try:
        assert backend._client._trust_env is False
    finally:
        backend.close()


@respx.mock
def test_connection_sends_system_role_by_default():
    """连接测试默认发送与真实翻译一致的 system 消息结构。"""
    route = respx.post('https://example.com/v1/chat/completions').mock(
        return_value=httpx.Response(200, text=_SSE_OK)
    )
    run_connection_test('https://example.com/v1', 'key', 'test')
    sent = json.loads(route.calls.last.request.content)
    assert sent['messages'][0]['role'] == 'system'
    assert len(sent['messages']) == 2


@respx.mock
def test_connection_merge_system():
    """use_system_role=False 时连接测试发送单条 user 消息。"""
    route = respx.post('https://example.com/v1/chat/completions').mock(
        return_value=httpx.Response(200, text=_SSE_OK)
    )
    run_connection_test(
        'https://example.com/v1', 'key', 'test', use_system_role=False
    )
    sent = json.loads(route.calls.last.request.content)
    assert len(sent['messages']) == 1
    assert sent['messages'][0]['role'] == 'user'


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
