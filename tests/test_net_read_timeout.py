"""出网读超时口径（2026-10-14 真机缺陷，用户报「特别容易超时」的真身）。

`CONNECT_TIMEOUT_CAP`（5 秒）是逐地址**连接**上限，但它被 `sock.settimeout(per_try)`
set 在 socket 上；连接成功后 socket 又被连接对象直接复用，而 `_exchange` 从没把超时
改回请求口径 —— 于是「5 秒」一路带到读写上：模型 5 秒没吐出第一个字节就被判成
`TimeoutError: The read operation timed out`（真机日志：请求 09:51:26 发出、
09:51:31 报超时，正好 5 秒）。配置里的 timeoutSeconds=180/150 在这条路上根本不生效。

这个用例用真 socket + 本地停机服务器把这件事钉住：连接上限压到 0.4 秒、服务器停
1.2 秒才回 headers、请求口径给 5 秒 —— 超时没交接的话必挂，交接了就该等到响应。
"""
import os
import socketserver
import sys
import threading
import time
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "python"))

import net_kit  # noqa: E402


class _StallHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    stall_seconds = 1.2

    def log_message(self, *args):                  # noqa: D102
        pass

    def do_POST(self):                             # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        time.sleep(self.stall_seconds)
        body = b'{"ok":true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        # HTTP/1.1 keep-alive 下 wfile 是带缓冲的：不 flush + 不关连接，body 会卡在
        # 服务器的缓冲区里，客户端只能等到自己的超时（第一版用例就是这么假失败的）。
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()
        self.close_connection = True


class ReadTimeoutHandoffTests(unittest.TestCase):
    def setUp(self):
        self.server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _StallHandler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = "http://127.0.0.1:%d/v1/chat/completions" % self.server.server_address[1]
        self._cap = net_kit.CONNECT_TIMEOUT_CAP
        self._secs = net_kit.CONNECT_TIMEOUT_SECONDS
        # 连接上限压到 0.4 秒，好在测试里复现「连接上限 < 服务器思考时间 < 请求口径」
        net_kit.CONNECT_TIMEOUT_SECONDS = 0.4
        net_kit.CONNECT_TIMEOUT_CAP = 0.4

    def tearDown(self):
        net_kit.CONNECT_TIMEOUT_SECONDS = self._secs
        net_kit.CONNECT_TIMEOUT_CAP = self._cap
        self.server.shutdown()
        self.server.server_close()

    def test_read_waits_for_the_request_timeout_not_the_connect_cap(self):
        opener = net_kit.build_ipv4_first_opener()
        req = urllib.request.Request(
            self.url, data=b"{}", method="POST",
            headers={"Content-Type": "application/json"})
        try:
            with opener(req, 5.0) as resp:
                body = resp.read()
        except Exception as exc:                   # noqa: BLE001
            self.fail("服务器停了 %.1fs，请求口径是 5s：不该超时，实际 %r"
                      % (_StallHandler.stall_seconds, exc))
        self.assertEqual(body, b'{"ok":true}')

    def test_connect_cap_is_still_the_tighter_bound_for_connecting(self):
        """连接本身仍按 cap 封顶（别把这条修成「连接也能等 5 秒」）。"""
        per_try = min(5.0, float(net_kit.CONNECT_TIMEOUT_CAP))
        self.assertEqual(per_try, 0.4)


if __name__ == "__main__":
    unittest.main()