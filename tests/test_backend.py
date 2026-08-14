"""后端客户端测试（用 respx mock HTTP）。"""

from __future__ import annotations

import json

import httpx
import respx

# 别名导入：test_connection 以 test_ 开头，直接导入会被 pytest 误收集为用例
from app.core.backend import (
    BackendError,
    OpenAIBackend,
    StreamCancelled,
    fetch_models,
)
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
def test_list_models_parses_data():
    """GET /models 解析 data 列表中的模型 id，去重并跳过无效项。"""
    payload = json.dumps({
        'object': 'list',
        'data': [
            {'id': 'qwen2.5:7b', 'object': 'model'},
            {'id': 'llama3.1:8b', 'object': 'model'},
            {'id': 'qwen2.5:7b'},  # 重复，应去重
            'not-a-dict',  # 非 dict，应跳过
            {'object': 'model'},  # 缺 id，应跳过
            {'id': '  '},  # 空白 id，应跳过
        ],
    })
    route = respx.get('https://example.com/v1/models').mock(
        return_value=httpx.Response(200, text=payload)
    )
    models = fetch_models('https://example.com/v1', 'key')
    assert models == ['qwen2.5:7b', 'llama3.1:8b']
    # 携带鉴权头请求
    assert route.calls.last.request.headers['authorization'] == 'Bearer key'


@respx.mock
def test_list_models_error_status():
    """后端返回 4xx 时抛 BackendError 含状态码。"""
    respx.get('https://example.com/v1/models').mock(
        return_value=httpx.Response(404, text='not found')
    )
    try:
        fetch_models('https://example.com/v1', 'key')
        assert False, '应抛异常'
    except BackendError as e:
        assert e.status_code == 404
        assert '获取模型列表失败' in str(e)


@respx.mock
def test_list_models_connection_error():
    """连接失败时抛 BackendError。"""
    respx.get('https://example.com/v1/models').mock(
        side_effect=httpx.ConnectError('refused')
    )
    try:
        fetch_models('https://example.com/v1', 'key')
        assert False, '应抛异常'
    except BackendError as e:
        assert '无法连接' in str(e)


@respx.mock
def test_list_models_bad_format():
    """200 但响应缺少 data 列表时抛 BackendError。"""
    respx.get('https://example.com/v1/models').mock(
        return_value=httpx.Response(200, text='{"models": []}')
    )
    try:
        fetch_models('https://example.com/v1', 'key')
        assert False, '应抛异常'
    except BackendError as e:
        assert 'data' in str(e)


@respx.mock
def test_chat_stream_cancel():
    """cancel() 中断流式请求：抛 StreamCancelled，不再吐后续 token。"""
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

    try:
        backend.chat_stream([], on_token=on_token)
        assert False, "cancel 后应抛 StreamCancelled"
    except StreamCancelled:
        pass
    # 取消后应只收到部分 token
    assert tokens == ['A']
    backend.close()


def test_cancel_aborts_and_backend_reusable():
    """cancel() 关闭连接可打断阻塞读取，且后端对象随后可复用。

    模拟"模型加载中"：服务端接受连接但长时间不响应；cancel()
    关闭连接后阻塞的读取应立即抛错退出，而不是干等超时。
    """
    import socket
    import threading
    import time

    from app.core.backend import StreamCancelled as Cancelled

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]

    def hang():
        conn, _ = srv.accept()
        try:
            conn.recv(65536)  # 读请求但不响应，模拟模型加载挂起
            time.sleep(60)  # 保持连接打开，等 cancel 来打断
        except OSError:
            pass

    threading.Thread(target=hang, daemon=True).start()

    backend = OpenAIBackend(f"http://127.0.0.1:{port}/v1", "", "m", timeout=30)
    result: dict = {"state": "running"}

    def worker():
        try:
            backend.chat_stream([{"role": "user", "content": "hi"}])
            result["state"] = "done"
        except Cancelled:
            result["state"] = "cancelled"
        except Exception as e:  # noqa: BLE001 - 测试需捕获一切
            result["state"] = f"error: {type(e).__name__}"

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    time.sleep(0.5)  # 让 worker 进入阻塞读取
    assert result["state"] == "running", "请求应先处于阻塞等待状态"

    backend.cancel()  # 关闭连接 → 阻塞读取立即被打断
    t.join(timeout=5)
    assert not t.is_alive(), "cancel 后阻塞的读取应立即退出"
    assert result["state"] == "cancelled", f"实际状态: {result['state']}"
    srv.close()
