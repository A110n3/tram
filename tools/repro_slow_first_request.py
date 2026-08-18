"""临时复现脚本：模拟"后端首次请求需加载模型"的慢响应。

服务端行为可选：
- hold: 接受连接后先 sleep N 秒再发送响应头 + SSE（模拟加载完才回头）
- headers_then_hold: 立即发送 200 响应头，sleep N 秒后再发 SSE 体

用 app.core.backend.OpenAIBackend + app.core.translator.Translator
走与真实翻译一致的管线，观察结果是否送达。
"""

from __future__ import annotations

import socket
import sys
import threading
import time

from app.core.backend import OpenAIBackend
from app.core.translator import Translator

MODE = sys.argv[1] if len(sys.argv) > 1 else "hold"
DELAY = float(sys.argv[2]) if len(sys.argv) > 2 else 20.0

SSE = (
    'data: {"choices":[{"delta":{"content":"Hello"}}]}\n\n'
    'data: {"choices":[{"delta":{"content":" world"}}]}\n\n'
    'data: [DONE]\n\n'
)


def handle(conn: socket.socket) -> None:
    try:
        conn.recv(65536)  # 读掉请求
        if MODE == "headers_then_hold":
            conn.sendall(
                b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                b"Transfer-Encoding: chunked\r\nConnection: close\r\n\r\n"
            )
        print(f"[server] 收到请求，{DELAY}s 后响应（模拟模型加载）", flush=True)
        time.sleep(DELAY)
        if MODE == "hold":
            conn.sendall(
                b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                b"Transfer-Encoding: chunked\r\nConnection: close\r\n\r\n"
            )
        for chunk in SSE.split("\n\n"):
            if not chunk:
                continue
            body = (chunk + "\n\n").encode()
            conn.sendall(b"%x\r\n" % len(body) + body + b"\r\n")
            time.sleep(0.05)
        conn.sendall(b"0\r\n\r\n")
        print("[server] 响应发送完毕", flush=True)
    except OSError as e:
        print(f"[server] 连接异常: {e}", flush=True)
    finally:
        conn.close()


def main() -> None:
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(4)
    port = srv.getsockname()[1]
    print(f"[server] 监听 127.0.0.1:{port} mode={MODE} delay={DELAY}s", flush=True)

    def accept_loop() -> None:
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            threading.Thread(target=handle, args=(conn,), daemon=True).start()

    threading.Thread(target=accept_loop, daemon=True).start()

    backend = OpenAIBackend(f"http://127.0.0.1:{port}/v1", "", "m", timeout=180)
    config = {
        "backend": {"use_system_role": True, "temperature": 0.2, "max_tokens": 2048},
        "translation": {},
    }
    translator = Translator(backend, config)

    tokens: list[str] = []
    t0 = time.monotonic()
    try:
        result = translator.translate(
            "test", on_token=lambda t: tokens.append(t)
        )
        print(
            f"[client] {time.monotonic() - t0:.1f}s 后拿到结果: {result!r}"
            f"（token 数 {len(tokens)}）",
            flush=True,
        )
    except Exception as e:
        print(
            f"[client] {time.monotonic() - t0:.1f}s 后异常: {type(e).__name__}: {e}",
            flush=True,
        )
    finally:
        backend.close()
        srv.close()


if __name__ == "__main__":
    main()
