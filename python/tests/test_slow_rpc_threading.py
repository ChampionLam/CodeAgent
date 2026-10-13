"""慢 RPC 不许堵死侧车主循环（2026-10-13 用户现场故障回归）。

故障链（行号以 main 分支 2b3f43e 为准）：
  1. sidecar 主循环（sidecar.py:2696）对每个 req **同步内联**执行
     ``write_frame(handle_request(req))``，只有 chat.*（sidecar.py:2407）
     被扔进工作线程。
  2. ``models.fetch``（sidecar.py:1225）调 modelconfig.fetch_remote_models_detailed()
     底层 ``modelconfig._fetch_models_payload``（modelconfig.py:210）用 urllib
     打厂商 GET /models，默认 timeout=20s。
  3. 公司网络里这个请求会被黑洞（详见下方 IPv6 根因），主循环最长堵 20 秒，
     期间 ping 不回 pong。
  4. Electron 侧心跳 watchdog 收不到 pong → 杀进程重启 → 3 次后 crashed，
     界面所有模型接口报 ``sidecar not ready: crashed`` 并重启循环闪烁。

本文件三类断言（红→绿证据见测试名）：
  * SlowRpcDoesNotBlockMainLoop —— 慢 handler 在 models.fetch 路径上睡 3 秒，
    期间 ping 必须立刻回 pong（修复前：ping 要等 handler 睡完才回）。
  * SlowRpcLogging —— 慢于 2 秒的请求必须留痕（开始/耗时/结果）。
  * NetKit IPv4-first —— IPv6 黑洞时回落 IPv4；TCP 连接超时 ≤5 秒。
"""
from __future__ import annotations

import json
import os
import queue
import re
import socket
import sys
import tempfile
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
PY = os.path.dirname(HERE)                       # python/
sys.path.insert(0, PY)

import logging_kit  # noqa: E402
import modelconfig as MC  # noqa: E402
import sidecar  # noqa: E402


PV = sidecar.SIDECAR_PROTOCOL_VERSION


def _req(req_id: str, method: str, params: dict | None = None) -> dict:
    return {"type": "req", "id": req_id, "method": method,
            "params": {"protocolVersion": PV, **(params or {})}}


class _PipedStdout:
    """捕获 write_frame 输出的假 stdout（线程安全收集）。"""

    def __init__(self) -> None:
        self.frames: list[dict] = []
        self.lock = threading.Lock()
        self.box: queue.Queue = queue.Queue()

    def write(self, payload: dict, out=None) -> None:
        with self.lock:
            self.frames.append(payload)
        self.box.put(payload)

    def next_response(self, timeout: float) -> dict:
        try:
            return self.box.get(timeout=timeout)
        except queue.Empty:
            raise AssertionError(f"no response frame within {timeout}s")


class _FakeLogger:
    """记录 log() 调用的假 logger（不落盘，线程安全）。"""

    def __init__(self) -> None:
        self.entries: list[tuple[str, str]] = []
        self.lock = threading.Lock()

    def log(self, level: str, msg: str) -> None:
        with self.lock:
            self.entries.append((level, msg))


class SlowRpcTestBase(unittest.TestCase):
    """公共脚手架：隔离数据目录 + 假 stdout + 假日志。"""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="slowrpc-")
        self._saved_data_dir = sidecar._DATA_DIR
        sidecar._DATA_DIR = os.path.join(self.tmp, "data")
        self._saved_logs = []
        self._orig_log = sidecar.log
        self.fake_log = _FakeLogger()
        sidecar.log = self.fake_log.log        # type: ignore[assignment]
        self.out = _PipedStdout()
        self._saved_write = sidecar.write_frame
        # write_frame 已带 _WRITE_LOCK；这里只替换输出端点，锁仍在。
        self._orig_write_unlocked = sidecar._write_frame_unlocked
        sidecar._write_frame_unlocked = self.out.write

    def tearDown(self) -> None:
        sidecar._write_frame_unlocked = self._orig_write_unlocked
        sidecar.write_frame = self._saved_write
        sidecar.log = self._orig_log          # type: ignore[assignment]
        sidecar._DATA_DIR = self._saved_data_dir
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)


class SlowRpcDoesNotBlockMainLoop(SlowRpcTestBase):
    """核心回归：models.fetch 卡在慢网络请求上时，主循环必须还能回 ping。"""

    SLOW_SECONDS = 3.0

    def _install_slow_fetch(self, seen: dict) -> None:
        """把 fetch_remote_models_detailed 换成睡 3 秒的假实现。"""

        def slow(*a, **kw):
            seen["started"] = time.monotonic()
            time.sleep(self.SLOW_SECONDS)
            return ["m1", "m2"], {}
        self._orig = sidecar.modelconfig.fetch_remote_models_detailed
        sidecar.modelconfig.fetch_remote_models_detailed = slow

    def tearDown(self) -> None:
        if getattr(self, "_orig", None):
            sidecar.modelconfig.fetch_remote_models_detailed = self._orig
        super().tearDown()

    def test_ping_answered_while_models_fetch_is_slow(self):
        seen: dict = {}
        self._install_slow_fetch(seen)

        # 主循环收到 models.fetch 后，handler 在工作线程里跑；
        # 同一时刻主线程发 ping，必须在 handler 醒来之前回 pong。
        ack = sidecar.handle_request(_req("f1", "models.fetch", {"baseUrl": "https://x/v1"}))
        # 修复后：models.fetch 应立即 ack（dispatched=True），而不是睡 3 秒。
        started = time.monotonic()
        pong = sidecar.handle_request(_req("p1", "ping"))
        pong_latency = time.monotonic() - started
        self.assertLess(pong_latency, 1.0,
                        "ping 被慢 models.fetch 堵住了 %.2fs —— 主循环被阻塞"
                        % pong_latency)
        self.assertTrue(pong.get("ok"), pong)
        self.assertTrue(pong.get("result", {}).get("pong"), pong)

        # 最终结果帧必须带正确的 id 且内容正确（不串台）。
        final = self.out.next_response(timeout=self.SLOW_SECONDS + 5)
        self.assertEqual(final.get("id"), "f1", final)
        self.assertTrue(final.get("ok"), final)
        self.assertEqual(final.get("result", {}).get("models"), ["m1", "m2"])
        # 确认慢 handler 真的睡满 3 秒（不是没跑）。
        self.assertGreaterEqual(time.monotonic() - seen["started"],
                                self.SLOW_SECONDS - 0.1)

    def test_models_fetch_error_reaches_client_with_id(self):
        """失败也要在后台线程里转成带 id 的错误帧（401 等真实错误能回传）。"""

        def boom(*a, **kw):
            time.sleep(self.SLOW_SECONDS)
            raise MC.ModelFetchError("MODEL_FETCH_UNAUTHORIZED",
                                     "该厂商拒绝了这个 key（HTTP 401）")
        self._orig = sidecar.modelconfig.fetch_remote_models_detailed
        sidecar.modelconfig.fetch_remote_models_detailed = boom
        sidecar.handle_request(_req("e1", "models.fetch", {"baseUrl": "https://x/v1"}))
        frame = self.out.next_response(timeout=self.SLOW_SECONDS + 5)
        self.assertEqual(frame.get("id"), "e1", frame)
        self.assertFalse(frame.get("ok"), frame)
        self.assertEqual(frame["error"]["code"], "MODEL_FETCH_UNAUTHORIZED")


class SlowRpcLogging(SlowRpcTestBase):
    """慢于 2 秒的网络型请求必须有开始/耗时/结果三段留痕。"""

    def _entries_text(self) -> str:
        return "\n".join("%s %s" % (lv, msg) for lv, msg in self.fake_log.entries)

    def test_slow_fetch_logged_start_duration_and_outcome(self):
        def slow(*a, **kw):
            time.sleep(2.2)
            return ["m1"], {}
        orig = sidecar.modelconfig.fetch_remote_models_detailed
        sidecar.modelconfig.fetch_remote_models_detailed = slow
        try:
            sidecar.handle_request(_req("l1", "models.fetch", {"baseUrl": "https://x/v1"}))
            # 等后台线程把结果帧写完
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                if self.out.frames:
                    break
                time.sleep(0.05)
            text = self._entries_text()
            self.assertIn("models.fetch", text)
            self.assertIn("start", text.lower())
            self.assertIn("slow", text.lower())
            # 耗时必须以秒数形式出现（如 "2.2" / "2200ms" 都认）
            self.assertRegex(text, r"\d+\.\d+\s*s|\d+\s*ms")
            self.assertIn("ok", text.lower())
        finally:
            sidecar.modelconfig.fetch_remote_models_detailed = orig

    def test_fast_fetch_not_flagged_slow(self):
        def fast(*a, **kw):
            return ["m1"], {}
        orig = sidecar.modelconfig.fetch_remote_models_detailed
        sidecar.modelconfig.fetch_remote_models_detailed = fast
        try:
            sidecar.handle_request(_req("l2", "models.fetch", {"baseUrl": "https://x/v1"}))
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                if self.out.frames:
                    break
                time.sleep(0.05)
            text = self._entries_text()
            self.assertNotIn("SLOW", text.upper())
        finally:
            sidecar.modelconfig.fetch_remote_models_detailed = orig


def _frontend_timeout_ms(name: str) -> int:
    """从前端源码里读它实际生效的超时，别在本子里抄一份数字（会漂移）。"""
    ts = os.path.join(os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))), "electron", "sidecarClient.ts")
    with open(ts, encoding="utf-8") as fh:
        body = fh.read()
    m = re.search(r"%s\s*\?\?\s*([0-9_]+)" % name, body)
    if not m:
        raise AssertionError("electron/sidecarClient.ts 里找不到 %s 的默认值" % name)
    return int(m.group(1).replace("_", ""))


class ModelFetchTimeoutContract(unittest.TestCase):
    """超时口径是推导关系，不是散落的魔数：

        逐地址连接上限 < 前端心跳判死阈值        （慢连接不能被当成侧车死了）
        侧车出网总超时 < 前端 RPC 单请求超时      （真实错误要能回传）
        侧车出网总超时 > 逐地址连接上限          （要留得下回落余量）
    """

    def test_model_fetch_uses_named_constant_not_a_literal(self):
        """按参数名取默认值（用下标取会撞上 api_key 的空串默认）。"""
        import inspect

        import net_kit
        for fn in (MC._fetch_models_payload, MC.fetch_remote_models,
                   MC.fetch_remote_models_detailed):
            default = inspect.signature(fn).parameters["timeout"].default
            self.assertEqual(
                default, net_kit.HTTP_TIMEOUT_SECONDS,
                "%s 的 timeout 默认值必须引用 net_kit 的常量，不能写字面量"
                % fn.__name__)

    def test_connect_cap_stays_under_frontend_heartbeat_death(self):
        import net_kit
        death_ms = _frontend_timeout_ms("heartbeatTimeoutMs")
        self.assertLess(net_kit.CONNECT_TIMEOUT_CAP * 1000, death_ms,
                        "逐地址连接上限 %.1fs 必须小于前端心跳判死 %.1fs"
                        % (net_kit.CONNECT_TIMEOUT_CAP, death_ms / 1000.0))

    def test_sidecar_http_timeout_stays_under_frontend_rpc_timeout(self):
        import net_kit
        rpc_ms = _frontend_timeout_ms("requestTimeoutMs")
        self.assertLess(net_kit.HTTP_TIMEOUT_SECONDS * 1000, rpc_ms,
                        "侧车出网总超时 %.1fs 必须小于前端 RPC 单请求超时 %.1fs"
                        % (net_kit.HTTP_TIMEOUT_SECONDS, rpc_ms / 1000.0))
        self.assertGreater(net_kit.HTTP_TIMEOUT_SECONDS,
                           net_kit.CONNECT_TIMEOUT_CAP,
                           "总超时要留得下至少一次回落")


class NetKitIPv4FallbackTest(unittest.TestCase):
    """IPv4 优先 + IPv6 黑洞快速回落（公司网 AAAA 根因）。"""

    def _fake_resolver(self, order):
        def resolve(host, port):
            return list(order)
        return resolve

    def _fake_socket_factory(self, fail_ips: set[str], connect_log: list):
        def factory(family, addr):
            sock = _FakeSocket(family, addr, fail_ips, connect_log)
            return sock
        return factory

    def test_ipv4_first_skips_ipv6_blackhole(self):
        """DNS 把 IPv6 排前面、且它是黑洞时，IPv4 优先就该直接连上，
        根本不去碰那个黑洞（否则要白等一个连接超时）。"""
        from net_kit import connect_prefer_ipv4
        log: list = []
        sock = connect_prefer_ipv4(
            ("llm.example.maas.aliyuncs.com", 443),
            resolver=self._fake_resolver(["2408:400a::1", "39.105.92.18"]),
            socket_factory=self._fake_socket_factory({"2408:400a::1"}, log),
            connect_timeout=5.0,
        )
        self.assertIsNotNone(sock)
        self.assertEqual(sock.addr, ("39.105.92.18", 443))
        self.assertEqual(log, [("39.105.92.18", 443)],
                         "IPv4 可用时不该去试 IPv6 黑洞")

    def test_ipv6_used_when_ipv4_unavailable(self):
        """IPv4 挂了的网络里仍然要能回落 IPv6（不能只会用 IPv4）。"""
        from net_kit import connect_prefer_ipv4
        log: list = []
        sock = connect_prefer_ipv4(
            ("llm.example.maas.aliyuncs.com", 443),
            resolver=self._fake_resolver(["2408:400a::1", "39.105.92.18"]),
            socket_factory=self._fake_socket_factory({"39.105.92.18"}, log),
            connect_timeout=5.0,
        )
        self.assertEqual(sock.addr, ("2408:400a::1", 443))
        self.assertEqual(log, [("39.105.92.18", 443), ("2408:400a::1", 443)])

    def test_ipv4_tried_first_when_available(self):
        from net_kit import connect_prefer_ipv4
        log: list = []
        sock = connect_prefer_ipv4(
            ("llm.example.maas.aliyuncs.com", 443),
            resolver=self._fake_resolver(["39.105.92.18", "2408:400a::1"]),
            socket_factory=self._fake_socket_factory(set(), log),
            connect_timeout=5.0,
        )
        self.assertIsNotNone(sock)
        self.assertEqual(sock.addr, ("39.105.92.18", 443))
        self.assertEqual(len(log), 1, "IPv4 可用时不该再试 IPv6")

    def test_connect_timeout_capped_at_5s(self):
        from net_kit import connect_prefer_ipv4
        log: list = []
        captured: dict = {}
        factory = self._fake_socket_factory({"2408:400a::1", "39.105.92.18"}, log)

        def timed_factory(family, addr):
            sock = factory(family, addr)
            orig = sock.settimeout
            def settimeout(v):
                captured[addr[0]] = v
                orig(v)
            sock.settimeout = settimeout
            return sock
        # 传一个 60s 的总超时，逐地址连接超时也必须被压到 ≤5s
        with self.assertRaises(OSError):
            connect_prefer_ipv4(
                ("x.example.com", 443),
                resolver=self._fake_resolver(["2408:400a::1", "39.105.92.18"]),
                socket_factory=timed_factory,
                connect_timeout=60.0,
            )
        for _addr, t in captured.items():
            self.assertLessEqual(t, 5.0,
                                 "逐地址 TCP 连接超时必须 ≤5s（得到 %.1fs）" % t)

    def test_resolver_failure_raises_gaierror(self):
        from net_kit import connect_prefer_ipv4

        def bad_resolver(host, port):
            raise socket.gaierror("no address")

        with self.assertRaises(socket.gaierror):
            connect_prefer_ipv4(
                ("x", 443), resolver=bad_resolver,
                socket_factory=self._fake_socket_factory(set(), []),
                connect_timeout=5.0)

    def test_last_error_surfaces_when_all_fail(self):
        from net_kit import connect_prefer_ipv4
        log: list = []
        with self.assertRaises(OSError) as ctx:
            connect_prefer_ipv4(
                ("x.example.com", 443),
                resolver=self._fake_resolver(["2001:db8::1", "198.51.100.5"]),
                socket_factory=self._fake_socket_factory(
                    {"2001:db8::1", "198.51.100.5"}, log),
                connect_timeout=5.0)
        # IPv4 优先：198.51.100.5 先试，IPv6 随后（两边都失败才抛）
        self.assertEqual(log, [("198.51.100.5", 443), ("2001:db8::1", 443)])


class _FakeSocket:
    """假 socket：黑名单地址 connect() 抛 timeout，否则成功。"""

    def __init__(self, family, addr, fail_ips: set[str], log: list):
        self.family = family
        self.addr = addr
        self._fail = addr[0] in fail_ips
        self._log = log

    def settimeout(self, v):
        self.timeout = v

    def connect(self, addr):
        self._log.append(addr)
        if self._fail:
            raise socket.timeout("timed out")
        return None

    def close(self):
        pass


class PinnedHTTPSOpenerTest(unittest.TestCase):
    """出网模型请求走 IPv4 优先连接，Host 与 TLS SNI 不因换地址失效。"""

    def test_urlopen_uses_pinned_connection(self):
        import urllib.request
        import net_kit
        seen: dict = {}

        def fake_connect_prefer(host, port, timeout, **kw):
            seen["host"] = host
            seen["port"] = port
            seen["timeout"] = timeout
            return "fake-sock"

        class _FakeHTTPResponse:
            def __init__(self):
                self.status = 200
                self._body = json.dumps({"data": [{"id": "m"}]}).encode()
            def read(self, n=-1):
                return self._body
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False
            def close(self):
                pass

        def fake_open(req, timeout):
            seen["url"] = req.full_url
            return _FakeHTTPResponse()

        opener = net_kit.build_ipv4_first_opener(
            connect_fn=fake_connect_prefer, http_open=fake_open)
        resp = opener(urllib.request.Request("https://llm.example.com/v1/models"),
                      timeout=15)
        body = json.loads(resp.read().decode())
        self.assertEqual(body["data"][0]["id"], "m")
        self.assertEqual(seen["host"], "llm.example.com")
        self.assertEqual(seen["port"], 443)


if __name__ == "__main__":
    unittest.main()
