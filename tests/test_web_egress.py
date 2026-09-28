"""web/egress.py 单测：出口策略必须靠断言钉死，不能靠「应该没问题」。

零真网络：resolver 和 opener 都是注入的假实现，所以这些用例在离线机器上
也能跑，而且能精确断言「连的是哪个 IP」。
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "python"))

from web import egress  # noqa: E402


PUBLIC = ["93.184.216.34"]
PRIVATE = ["127.0.0.1"]
LAN = ["192.168.7.10"]
META = ["169.254.169.254"]


def resolver_for(*addresses, record=None):
    def fake(host, port):
        if record is not None:
            record.append((host, port))
        return list(addresses)
    return fake


class FakeResponse:
    """Minimal http.client.HTTPResponse stand-in used by the opener seam."""

    def __init__(self, body=b"hello", status=200, headers=None, chunk=None):
        self._body = body
        self.status = status
        self._headers = {k.lower(): v for k, v in (headers or {}).items()}
        self._chunk = chunk or 8192
        self._offset = 0

    def getheader(self, name, default=None):
        return self._headers.get((name or "").lower(), default)

    def read(self, size=None):
        if size is None:
            size = len(self._body)
        size = min(size, self._chunk)
        piece = self._body[self._offset:self._offset + size]
        self._offset += len(piece)
        return piece


def opener_returning(*responses, record=None):
    """Returns an opener that hands out the given responses in order."""
    queue = list(responses)

    def fake(**kwargs):
        if record is not None:
            record.append(kwargs)
        return queue.pop(0) if queue else FakeResponse()

    return fake


class ValidateUrlTest(unittest.TestCase):
    def test_accepts_http_and_https(self):
        for url in ("http://example.com/a?b=1", "https://example.com/"):
            self.assertEqual(egress.validate_url(url).scheme, "https" if "https" in url else "http")

    def test_rejects_non_http_schemes(self):
        for url in ("file:///etc/passwd", "ftp://example.com/x", "gopher://x/1",
                    "javascript:alert(1)", "data:text/html,<b>x"):
            with self.assertRaises(egress.EgressError) as ctx:
                egress.validate_url(url)
            self.assertEqual(ctx.exception.code, "BLOCKED_SCHEME")

    def test_rejects_credentials_in_url(self):
        with self.assertRaises(egress.EgressError) as ctx:
            egress.validate_url("https://user:pass@example.com/")
        self.assertEqual(ctx.exception.code, "BLOCKED_HOST")

    def test_rejects_empty_and_non_string(self):
        for bad in ("", "   ", None, 12):
            with self.assertRaises(egress.EgressError) as ctx:
                egress.validate_url(bad)  # type: ignore[arg-type]
            self.assertEqual(ctx.exception.code, "BAD_REQUEST")

    def test_rejects_overlong_url(self):
        with self.assertRaises(egress.EgressError) as ctx:
            egress.validate_url("https://example.com/" + "a" * 3000)
        self.assertEqual(ctx.exception.code, "BAD_REQUEST")

    def test_rejects_hostname_less_url(self):
        with self.assertRaises(egress.EgressError) as ctx:
            egress.validate_url("https:///path")
        self.assertEqual(ctx.exception.code, "BAD_REQUEST")


class SecretInUrlTest(unittest.TestCase):
    def test_blocks_credential_style_query_params(self):
        for url in ("https://x.com/?api_key=abcdefghijklmnop",
                    "https://x.com/a?token=zzz",
                    "https://x.com/?access_token=q"):
            with self.assertRaises(egress.EgressError) as ctx:
                egress.validate_url(url)
            self.assertEqual(ctx.exception.code, "BLOCKED_SECRET")

    def test_blocks_credential_shaped_values_without_param_names(self):
        with self.assertRaises(egress.EgressError) as ctx:
            egress.validate_url("https://x.com/sk-abcdefghijklmnopqrstuvwxyz123456")
        self.assertEqual(ctx.exception.code, "BLOCKED_SECRET")

    def test_blocks_double_encoded_secret_param(self):
        # %2560 解码两次才露出 ?key=；只解一次的实现会放它过去。
        with self.assertRaises(egress.EgressError) as ctx:
            egress.validate_url("https://x.com/a%3Fkey%3Dvalue1234567890")
        self.assertEqual(ctx.exception.code, "BLOCKED_SECRET")

    def test_blocks_env_secret_value(self):
        os.environ["WORKBUDDY_TEST_API_KEY"] = "s3cret-value-that-is-long"
        try:
            with self.assertRaises(egress.EgressError) as ctx:
                egress.validate_url("https://x.com/?q=s3cret-value-that-is-long")
            self.assertEqual(ctx.exception.code, "BLOCKED_SECRET")
        finally:
            os.environ.pop("WORKBUDDY_TEST_API_KEY", None)

    def test_plain_urls_pass(self):
        self.assertIsNone(egress.looks_like_secret("https://x.com/search?q=weather"))


class HostPolicyTest(unittest.TestCase):
    def test_blocks_localhost_names(self):
        for host in ("localhost", "LOCALHOST", "localhost.", "api.localhost"):
            with self.assertRaises(egress.EgressError) as ctx:
                egress.check_host_allowed(host, 80, resolver=resolver_for(*PUBLIC))
            self.assertEqual(ctx.exception.code, "BLOCKED_HOST")

    def test_blocks_private_loopback_and_metadata(self):
        for addrs in (PRIVATE, LAN, META, ["::1"], ["fd00::1"], ["0.0.0.0"]):
            with self.assertRaises(egress.EgressError) as ctx:
                egress.check_host_allowed("evil.example", 80, resolver=resolver_for(*addrs))
            self.assertEqual(ctx.exception.code, "BLOCKED_PRIVATE")

    def test_blocks_when_any_resolved_address_is_private(self):
        # 一个公网 + 一个内网 = 拒绝：不能只查第一个地址。
        with self.assertRaises(egress.EgressError) as ctx:
            egress.check_host_allowed("mix.example", 80,
                                      resolver=resolver_for(*PUBLIC, *LAN))
        self.assertEqual(ctx.exception.code, "BLOCKED_PRIVATE")

    def test_pins_first_public_address(self):
        pinned = egress.check_host_allowed("ok.example", 443, resolver=resolver_for(*PUBLIC))
        self.assertEqual(pinned, "93.184.216.34")

    def test_dns_failure_is_reported(self):
        def boom(host, port):
            raise egress.EgressError("DNS_FAILED", "nope")
        with self.assertRaises(egress.EgressError) as ctx:
            egress.check_host_allowed("nx.example", 80, resolver=boom)
        self.assertEqual(ctx.exception.code, "DNS_FAILED")

    def test_allowlist_lets_a_named_host_through(self):
        os.environ["WEB_EGRESS_ALLOW_PRIVATE"] = "searx.lan"
        try:
            pinned = egress.check_host_allowed("searx.lan", 8080, resolver=resolver_for(*LAN))
            self.assertEqual(pinned, "192.168.7.10")
        finally:
            os.environ.pop("WEB_EGRESS_ALLOW_PRIVATE", None)

    def test_allowlist_does_not_leak_to_other_hosts(self):
        os.environ["WEB_EGRESS_ALLOW_PRIVATE"] = "searx.lan"
        try:
            with self.assertRaises(egress.EgressError):
                egress.check_host_allowed("other.lan", 80, resolver=resolver_for(*LAN))
        finally:
            os.environ.pop("WEB_EGRESS_ALLOW_PRIVATE", None)


class FetchPolicyTest(unittest.TestCase):
    def test_happy_path_returns_body_and_final_url(self):
        opener = opener_returning(FakeResponse(b"<html>hi</html>",
                                               headers={"Content-Type": "text/html; charset=utf-8"}))
        result = egress.fetch("https://example.com/page",
                              resolver=resolver_for(*PUBLIC), opener=opener)
        self.assertEqual(result.status, 200)
        self.assertEqual(result.body, b"<html>hi</html>")
        self.assertEqual(result.url, "https://example.com/page")
        self.assertFalse(result.truncated)

    def test_connects_to_the_pinned_ip_not_the_name(self):
        # 这是防 DNS rebinding 的实质：拿到的必须是解析结果里的字面 IP。
        seen = []
        opener = opener_returning(FakeResponse(headers={"Content-Type": "text/plain"}),
                                  record=seen)
        egress.fetch("https://example.com/x", resolver=resolver_for(*PUBLIC), opener=opener)
        self.assertEqual(seen[0]["pinned_ip"], "93.184.216.34")
        self.assertEqual(seen[0]["parsed"].hostname, "example.com")

    def test_follows_same_host_redirect_once(self):
        opener = opener_returning(
            FakeResponse(status=302, headers={"Location": "/moved"}),
            FakeResponse(b"done", headers={"Content-Type": "text/plain"}),
        )
        result = egress.fetch("https://example.com/page",
                              resolver=resolver_for(*PUBLIC), opener=opener)
        self.assertEqual(result.body, b"done")
        self.assertEqual(result.url, "https://example.com/moved")
        self.assertEqual(len(result.redirects), 1)

    def test_follows_cross_host_redirect_after_revalidation(self):
        # 跨站跳转必须放行：正经站点天天这么跳（open.bigmodel.cn →
        # docs.bigmodel.cn，2026-09-24 实测被老策略误拒）。安全性不靠「拒跨站」，
        # 靠每一跳重新解析 + 重新过策略，见下一个用例。
        seen_hosts = []

        def resolver(host, port):
            seen_hosts.append(host)
            return list(PUBLIC)

        opener = opener_returning(
            FakeResponse(status=302, headers={"Location": "https://docs.example.cn/guide"}),
            FakeResponse(b"docs", headers={"Content-Type": "text/html"}),
        )
        result = egress.fetch("https://example.com/page", resolver=resolver, opener=opener)
        self.assertEqual(result.url, "https://docs.example.cn/guide")
        self.assertIn("docs.example.cn", seen_hosts)
        self.assertEqual(len(result.redirects), 1)

    def test_rejects_redirect_to_a_blocked_name(self):
        opener = opener_returning(
            FakeResponse(status=302, headers={"Location": "http://localhost/admin"}))
        with self.assertRaises(egress.EgressError) as ctx:
            egress.fetch("https://example.com/page", resolver=resolver_for(*PUBLIC),
                         opener=opener)
        self.assertEqual(ctx.exception.code, "BLOCKED_HOST")

    def test_refuses_too_many_redirects(self):
        loop = [FakeResponse(status=302, headers={"Location": "/again"}) for _ in range(6)]
        with self.assertRaises(egress.EgressError) as ctx:
            egress.fetch("https://example.com/page", resolver=resolver_for(*PUBLIC),
                         opener=opener_returning(*loop))
        self.assertEqual(ctx.exception.code, "TOO_MANY_REDIRECTS")

    def test_redirect_target_is_revalidated(self):
        # 跳转目标如果指向内网地址，必须在第二次解析时被拦。
        calls = {"n": 0}

        def resolver(host, port):
            calls["n"] += 1
            return list(PRIVATE) if calls["n"] > 1 else list(PUBLIC)

        opener = opener_returning(FakeResponse(status=302, headers={"Location": "/x"}),
                                  FakeResponse(b"nope", headers={"Content-Type": "text/plain"}))
        with self.assertRaises(egress.EgressError) as ctx:
            egress.fetch("https://example.com/page", resolver=resolver, opener=opener)
        self.assertEqual(ctx.exception.code, "BLOCKED_PRIVATE")

    def test_refuses_unsupported_content_type(self):
        opener = opener_returning(FakeResponse(b"%PDF-1.4",
                                               headers={"Content-Type": "application/pdf"}))
        with self.assertRaises(egress.EgressError) as ctx:
            egress.fetch("https://example.com/a.pdf", resolver=resolver_for(*PUBLIC), opener=opener)
        self.assertEqual(ctx.exception.code, "UNSUPPORTED_TYPE")

    def test_refuses_http_error_status(self):
        opener = opener_returning(FakeResponse(b"nope", status=404,
                                               headers={"Content-Type": "text/html"}))
        with self.assertRaises(egress.EgressError) as ctx:
            egress.fetch("https://example.com/missing", resolver=resolver_for(*PUBLIC), opener=opener)
        self.assertEqual(ctx.exception.code, "HTTP_ERROR")

    def test_body_is_capped_even_when_server_keeps_streaming(self):
        huge = b"x" * 5000
        opener = opener_returning(FakeResponse(huge, headers={"Content-Type": "text/plain"},
                                               chunk=512))
        result = egress.fetch("https://example.com/big", resolver=resolver_for(*PUBLIC),
                              opener=opener, max_bytes=1024)
        self.assertEqual(len(result.body), 1024)
        self.assertTrue(result.truncated)

    def test_private_target_never_reaches_the_opener(self):
        def opener(**kwargs):                       # pragma: no cover - must not run
            raise AssertionError("opener called for a blocked host")
        with self.assertRaises(egress.EgressError):
            egress.fetch("http://router.lan/admin", resolver=resolver_for(*LAN), opener=opener)


class ReadCapTest(unittest.TestCase):
    def test_read_capped_stops_at_limit(self):
        class Chunky:
            def __init__(self):
                self.pieces = [b"a" * 10, b"b" * 10, b"c" * 10]

            def read(self, size=None):
                return self.pieces.pop(0) if self.pieces else b""

        self.assertEqual(egress._read_capped(Chunky(), 25), b"a" * 10 + b"b" * 10 + b"c" * 5)


if __name__ == "__main__":
    unittest.main()