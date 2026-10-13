"""net_kit 的响应对象必须能流式迭代 —— 这是聊天链路的硬要求。

背景（2026-10-14 真机 bug）：把出网改成 IPv4 优先后，响应对象换成自己写的
包装类，结果聊天直接报 `'_Response' object is not iterable`，因为 llm.py 是
`for raw in resp:` 逐行读 SSE 的。修法不是"补个 __iter__"，而是**不要预读响应
体** —— 预读会把流式变成"憋完整段再吐"，长回答看起来卡住、还会撞超时。

本测试用本地 SSE 服务验证两件事：
  1. 响应对象可迭代（逐行拿到 data: 行）
  2. 是真流式（分块之间 sleep，增量到达时刻应跟着散开，而不是一次到齐）
并覆盖 4xx → HTTPError 的老口径（错误码要能传给调用方）。
"""

from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import net_kit  # noqa: E402


def _serve_sse(port: int, pieces, gap: float, stop: threading.Event) -> None:
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", port))
    srv.listen(1)
    srv.settimeout(10)
    try:
        conn, _ = srv.accept()
    except socket.timeout:
        srv.close()
        return
    try:
        conn.recv(8192)
        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                     b"Transfer-Encoding: chunked\r\n\r\n")
        for piece in pieces:
            payload = json.dumps({"choices": [{"delta": {"content": piece}}]},
                                 ensure_ascii=False)
            line = ("data: %s\n\n" % payload).encode("utf-8")
            conn.sendall(b"%x\r\n" % len(line) + line + b"\r\n")
            time.sleep(gap)
        conn.sendall(b"0\r\n\r\n")
    finally:
        try:
            conn.close()
        except OSError:
            pass
        srv.close()
        stop.set()


def _serve_status(port: int, status: int, stop: threading.Event) -> None:
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", port))
    srv.listen(1)
    srv.settimeout(10)
    try:
        conn, _ = srv.accept()
    except socket.timeout:
        srv.close()
        return
    try:
        conn.recv(8192)
        body = b'{"error":{"message":"nope"}}'
        conn.sendall(b"HTTP/1.1 %d Forbidden\r\nContent-Length: %d\r\n"
                     b"Content-Type: application/json\r\n\r\n"
                     % (status, len(body)) + body)
    finally:
        try:
            conn.close()
        except OSError:
            pass
        srv.close()
        stop.set()


def _opener_for(port: int):
    opener = net_kit.build_ipv4_first_opener()
    return lambda timeout=10.0: opener(
        __import__("urllib.request", fromlist=["Request"]).Request(
            "http://127.0.0.1:%d/v1/chat/completions" % port),
        timeout=timeout)


class LiveResponseStreamsTest(unittest.TestCase):

    def test_response_is_iterable_and_streams_incrementally(self):
        import urllib.request
        port = 18761
        stop = threading.Event()
        pieces = ["连接", "正常", "流式", "测试"]
        t = threading.Thread(target=_serve_sse,
                             args=(port, pieces, 0.3, stop), daemon=True)
        t.start()
        time.sleep(0.15)

        opener = net_kit.build_ipv4_first_opener()
        req = urllib.request.Request(
            "http://127.0.0.1:%d/v1/chat/completions" % port)
        started = time.monotonic()
        stamps = []
        with opener(req, timeout=15) as resp:
            for _line in resp:              # 修复前这里直接 TypeError
                stamps.append(time.monotonic() - started)

        self.assertGreaterEqual(len(stamps), len(pieces),
                                "应逐行拿到全部 SSE 行")
        spread = stamps[-1] - stamps[0]
        self.assertGreater(
            spread, 0.4,
            "增量必须随分块到达（实测首末间隔 %.2fs）—— 预读实现会一次到齐"
            % spread)

    def test_http_error_carries_status_code(self):
        import urllib.error
        import urllib.request
        port = 18762
        stop = threading.Event()
        threading.Thread(target=_serve_status,
                         args=(port, 403, stop), daemon=True).start()
        time.sleep(0.15)

        opener = net_kit.build_ipv4_first_opener()
        req = urllib.request.Request(
            "http://127.0.0.1:%d/v1/models" % port)
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            opener(req, timeout=10)
        self.assertEqual(ctx.exception.code, 403,
                         "4xx 必须抛 HTTPError 且带原状态码（厂商真实错误要能回传）")


if __name__ == "__main__":
    unittest.main()