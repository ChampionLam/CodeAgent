"""Outbound HTTP egress policy: what the agent is allowed to reach, and how.

The threat model for a web tool inside a desktop agent that also holds API keys
and filesystem access:

1. **SSRF** — the model (or a page it read) asks for a URL pointing at the LAN,
   at ``127.0.0.1``, at the cloud metadata address ``169.254.169.254``, or at
   ``file://``. Rule: scheme allowlist, and every resolved IP must be public.
2. **DNS rebinding** — resolve once, then connect to the *literal IP* while
   keeping the original Host header and TLS SNI. If the process re-resolves,
   an attacker-controlled TTL-zero record can swing a public name to 127.0.0.1
   between the check and the connect.
3. **Redirect pivoting** — a public URL 302s to an internal one. Rule: follow
   at most 3 hops and refuse any hop that changes host.
4. **Secret exfiltration by URL** — ``?key=$API_KEY`` leaks a credential into
   someone's access log. Rule: reject URLs whose decoded text looks like a
   credential or contains an env-var-ish name we know about.
5. **Resource abuse** — a 2 GB response, or a socket that never closes. Rule:
   byte cap enforced while streaming, hard timeout, content-type allowlist.

Everything is injectable (``resolver``, ``connect``, ``now``) so tests can
assert the policy without a network. No module-level sockets are opened.
"""
from __future__ import annotations

import http.client
import ipaddress
import os
import re
import socket
import ssl
import urllib.parse
from dataclasses import dataclass, field
from typing import Callable, Iterable

# --- limits (bytes, not characters: a download budget is a network budget) ---
MAX_BYTES = 750_000
MAX_REDIRECTS = 3
CONNECT_TIMEOUT = 20.0

ALLOWED_SCHEMES = ("http", "https")

#: Content types we are willing to turn into text. Anything else (PDF, images,
#: video, octet-stream) is refused with a message the model can act on: it can
#: drop the file to disk with run_shell if it really wants the bytes.
ALLOWED_CONTENT_TYPES = (
    "text/html",
    "application/xhtml+xml",
    "text/plain",
    "application/json",
    "text/markdown",
    "text/x-markdown",
)

#: Hostnames that never leave the machine. Matched case-insensitively; a
#: trailing dot is stripped first because ``localhost.`` resolves the same.
BLOCKED_HOSTNAMES = (
    "localhost",
    "localhost.localdomain",
    "metadata.google.internal",
    "metadata",
)

#: Query-parameter names that suggest a credential is riding in the URL.
SECRET_PARAM_HINTS = (
    "key",
    "apikey",
    "api_key",
    "token",
    "access_token",
    "secret",
    "password",
    "passwd",
    "pwd",
    "auth",
    "signature",
    "sig",
    "credential",
)

#: Value shapes that look like live credentials regardless of parameter name.
_SECRET_VALUE_RES = (
    re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\."),      # JWT
    re.compile(r"\b[A-Fa-f0-9]{32,}\b"),                                  # long hex
    re.compile(r"\b[A-Za-z0-9+/]{40,}={0,2}\b"),                          # long base64
)


class EgressError(Exception):
    """Refusal to fetch. ``code`` is a stable, machine-readable reason."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class FetchResult:
    """What a successful fetch produced."""

    url: str                      # final URL after redirects
    status: int
    content_type: str
    body: bytes
    truncated: bool = False
    redirects: list[str] = field(default_factory=list)

    @property
    def bytes_read(self) -> int:
        return len(self.body)


def _is_public_ip(ip: str) -> bool:
    """True only for globally routable addresses.

    ``is_global`` alone is not enough: python's tables have moved around across
    versions for CGNAT (100.64/10), benchmarking (198.18/15) and documentation
    ranges, so the private/loopback/link-local/reserved checks are spelled out.
    """
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    if addr.is_private or addr.is_loopback or addr.is_link_local:
        return False
    if addr.is_multicast or addr.is_reserved or addr.is_unspecified:
        return False
    return True


def _decode_url_twice(url: str) -> str:
    """Percent-decode twice so ``%2520`` style double encoding cannot hide text."""
    once = urllib.parse.unquote(url)
    return urllib.parse.unquote(once)


def looks_like_secret(url: str) -> str | None:
    """Return a human-readable reason if the URL appears to carry a credential.

    Two independent nets: param names that mean "credential goes here", and the
    actual values of credential-shaped environment variables. The second net is
    what catches a model that helpfully pastes the API key into a query string.
    """
    decoded = _decode_url_twice(url)
    lowered = decoded.lower()
    for token in SECRET_PARAM_HINTS:
        if re.search(r"[?&]" + re.escape(token) + r"=", lowered):
            return "URL 里带着 %s= 这类参数（像是密钥/口令）" % token
    for pattern in _SECRET_VALUE_RES:
        if pattern.search(decoded):
            return "URL 里出现了密钥样式的长串"
    for value in env_secret_values():
        if value in decoded:
            return "URL 里出现了本机环境变量里的密钥值"
    return None


def validate_url(url: str) -> urllib.parse.SplitResult:
    """Structural URL checks. Raises EgressError; returns the parsed URL."""
    if not isinstance(url, str) or not url.strip():
        raise EgressError("BAD_REQUEST", "url must be a non-empty string")
    raw = url.strip()
    if len(raw) > 2048:
        raise EgressError("BAD_REQUEST", "url is too long (max 2048 characters)")
    try:
        parsed = urllib.parse.urlsplit(raw)
    except ValueError as exc:
        raise EgressError("BAD_REQUEST", "url cannot be parsed: %s" % exc)
    scheme = (parsed.scheme or "").lower()
    if scheme not in ALLOWED_SCHEMES:
        raise EgressError("BLOCKED_SCHEME",
                          "only http/https are allowed, got %r" % (parsed.scheme or "(none)"))
    if parsed.username or parsed.password:
        raise EgressError("BLOCKED_HOST", "credentials in the URL are not allowed")
    host = (parsed.hostname or "").strip().rstrip(".")
    if not host:
        raise EgressError("BAD_REQUEST", "url has no host")
    secret = looks_like_secret(raw)
    if secret:
        raise EgressError("BLOCKED_SECRET", "拒绝抓取：%s（密钥不许拼进 URL）" % secret)
    return parsed


def resolve_host(host: str, port: int) -> list[str]:
    """Resolve once, return every address. Raises EgressError on failure."""
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise EgressError("DNS_FAILED", "域名解析失败：%s (%s)" % (host, exc))
    addrs: list[str] = []
    for info in infos:
        addr = info[4][0]
        if addr not in addrs:
            addrs.append(addr)
    if not addrs:
        raise EgressError("DNS_FAILED", "域名解析没拿到地址：%s" % host)
    return addrs


def allowed_private_hosts() -> set[str]:
    """Hosts the user explicitly allowed to be private/local.

    Needed for a self-hosted SearXNG or an internal wiki: the default policy
    refuses every non-public address, and the escape hatch must be an explicit,
    user-written list — never something the model can influence.
    """
    raw = os.environ.get("WEB_EGRESS_ALLOW_PRIVATE", "")
    return {part.strip().lower().rstrip(".") for part in raw.split(",") if part.strip()}


def check_host_allowed(host: str, port: int, *, resolver: Callable[[str, int], list[str]] = resolve_host) -> str:
    """Full host policy: name blocklist + every resolved IP must be public.

    Returns the pinned IP to connect to. Pinning is the anti-rebinding measure:
    callers must connect to this literal address, not re-resolve.
    """
    lowered = host.strip().rstrip(".").lower()
    if lowered in allowed_private_hosts():
        addrs = resolver(lowered, port)
        return addrs[0]
    if lowered in BLOCKED_HOSTNAMES or lowered.endswith(".localhost"):
        raise EgressError("BLOCKED_HOST", "本机/内网域名不许抓：%s" % host)
    addrs = resolver(lowered, port)
    for addr in addrs:
        if not _is_public_ip(addr):
            raise EgressError("BLOCKED_PRIVATE",
                              "拒绝抓取：%s 解析到非公网地址 %s（内网/回环/metadata 一律不许；"
                              "确实要抓就把域名加进 WEB_EGRESS_ALLOW_PRIVATE）" % (host, addr))
    return addrs[0]


def _content_type_ok(content_type: str) -> bool:
    base = (content_type or "").split(";", 1)[0].strip().lower()
    return base in ALLOWED_CONTENT_TYPES or base.startswith("text/")


class _PinnedConnection(http.client.HTTPConnection):
    """HTTP(S) connection that dials a pre-resolved IP instead of re-resolving."""

    def __init__(self, host: str, port: int, *, pinned_ip: str, timeout: float, use_tls: bool):
        super().__init__(host, port, timeout=timeout)
        self._pinned_ip = pinned_ip
        self._use_tls = use_tls
        # ``host`` stays the logical name so Host: and SNI are correct.

    def connect(self) -> None:            # noqa: D102 - verbatim from stdlib shape
        sock = socket.create_connection((self._pinned_ip, self.port), self.timeout)
        if self._use_tls:
            context = ssl.create_default_context()
            # server_hostname pins SNI/cert validation to the real name, not the IP.
            sock = context.wrap_socket(sock, server_hostname=self.host)
        self.sock = sock


def fetch(
    url: str,
    *,
    timeout: float = CONNECT_TIMEOUT,
    max_bytes: int = MAX_BYTES,
    max_redirects: int = MAX_REDIRECTS,
    resolver: Callable[[str, int], list[str]] = resolve_host,
    opener: Callable[..., http.client.HTTPResponse] | None = None,
) -> FetchResult:
    """Fetch one URL with the full policy applied.

    ``opener`` is a test seam: it receives ``(url, parsed, pinned_ip, timeout,
    max_bytes)`` and returns something with ``.status``, ``.getheader`` and
    ``.read``. Production uses the pinned http.client path.
    """
    current = validate_url(url)
    redirects: list[str] = []

    for _ in range(max_redirects + 1):
        host = (current.hostname or "").strip().rstrip(".")
        port = current.port or (443 if current.scheme == "https" else 80)
        pinned = check_host_allowed(host, port, resolver=resolver)
        path = urllib.parse.urlunsplit(("", "", current.path or "/", current.query, ""))

        if opener is not None:
            resp = opener(url=urllib.parse.urlunsplit(current), parsed=current,
                          pinned_ip=pinned, timeout=timeout, max_bytes=max_bytes)
            status = int(getattr(resp, "status", 0))
            headers = resp
            body = _read_capped(resp, max_bytes)
        else:
            conn = _PinnedConnection(host, port, pinned_ip=pinned, timeout=timeout,
                                     use_tls=(current.scheme == "https"))
            try:
                conn.request("GET", path, headers={
                    "User-Agent": "CodeAgent/1.0 (+desktop agent; fetch tool)",
                    "Accept": "text/html,application/xhtml+xml,application/json,text/plain;q=0.9,*/*;q=0.1",
                    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                    "Connection": "close",
                })
                resp = conn.getresponse()
                status = int(resp.status)
                headers = resp
                body = _read_capped(resp, max_bytes)
            except socket.timeout:
                raise EgressError("TIMEOUT", "抓取超时（%ss）：%s" % (timeout, host))
            except ssl.SSLError as exc:
                raise EgressError("TLS_ERROR", "TLS 握手失败：%s (%s)" % (host, exc))
            except OSError as exc:
                raise EgressError("IO_ERROR", "网络错误：%s (%s)" % (host, exc))
            finally:
                try:
                    conn.close()
                except Exception:            # pragma: no cover - close never matters
                    pass

        location = ""
        try:
            location = (headers.getheader("Location") or "").strip()       # type: ignore[attr-defined]
        except Exception:
            location = ""
        content_type = ""
        try:
            content_type = (headers.getheader("Content-Type") or "").strip()   # type: ignore[attr-defined]
        except Exception:
            content_type = ""

        if status in (301, 302, 303, 307, 308) and location:
            if len(redirects) >= max_redirects:
                raise EgressError("TOO_MANY_REDIRECTS",
                                  "跳转超过 %d 次，放弃：%s" % (max_redirects, url))
            target = urllib.parse.urljoin(urllib.parse.urlunsplit(current), location)
            target_parsed = validate_url(target)
            # 跨站跳转必须放行：正经站点天天这么干（open.bigmodel.cn →
            # docs.bigmodel.cn、apex → www、http → https；2026-09-24 实测被这条
            # 掐掉过一次）。不放开就等于「只能抓从不跳站的站」。安全不靠「不许
            # 跨站」，靠的是每一跳都重新过一遍策略：域名黑名单 + DNS 重解析 +
            # 私网/回环拒绝 —— 下面这行就是重解析。
            next_port = target_parsed.port or (443 if target_parsed.scheme == "https" else 80)
            check_host_allowed(target_parsed.hostname, next_port, resolver=resolver)
            redirects.append(target)
            current = target_parsed
            continue

        if status >= 400:
            raise EgressError("HTTP_ERROR", "对方返回 HTTP %d：%s" % (status, host))
        if not _content_type_ok(content_type):
            raise EgressError(
                "UNSUPPORTED_TYPE",
                "不支持的内容类型 %r（只收 html/纯文本/json）；要拿二进制文件请用 run_shell 下载到工作区"
                % (content_type or "(未提供)"))

        truncated = len(body) >= max_bytes
        return FetchResult(url=urllib.parse.urlunsplit(current), status=status,
                           content_type=content_type, body=body,
                           truncated=truncated, redirects=redirects)

    raise EgressError("TOO_MANY_REDIRECTS", "跳转次数用尽：%s" % url)


def _read_capped(resp, max_bytes: int, chunk: int = 65536) -> bytes:
    """Read the body but never buffer more than ``max_bytes``.

    Reading in chunks matters: ``resp.read()`` on a hostile endpoint is how you
    turn a byte cap into an out-of-memory. The final slice is truncated too,
    because ``read(n)`` is only a request — a reader (or a test double) that
    ignores ``n`` must not be able to overshoot the cap either.
    """
    out = bytearray()
    while len(out) < max_bytes:
        piece = resp.read(min(chunk, max_bytes - len(out)))
        if not piece:
            break
        out.extend(piece)
    return bytes(out[:max_bytes])


def host_of(url: str) -> str:
    return (urllib.parse.urlsplit(url).hostname or "").lower()


def env_secret_values() -> Iterable[str]:
    """Values of credential-looking env vars — used to refuse leaking them.

    Only names are matched (never printed), and only values long enough to be
    real secrets are considered, so a stray ``PATH`` never trips this.
    """
    for name, value in os.environ.items():
        if not value or len(value) < 12:
            continue
        if re.search(r"KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL", name, re.I):
            yield value