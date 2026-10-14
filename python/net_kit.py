"""出网助手：IPv4 优先连接 + 固定地址的 HTTPS 客户端。

为什么需要这个模块
------------------
双栈网络里 IPv6 可能是黑洞（包被设备静默丢弃，不是拒绝）。标准库在这种
网络上会表现成"卡死"而不是"报错"：`socket.create_connection` 按
getaddrinfo 的顺序**串行**试地址，撞上黑洞就等到 socket 超时为止。实测
（公司网络，同一台机器、同一个端点）：IPv6 两个地址各等满超时，IPv4
0.079 秒就通；同机上的 anyio/httpx 客户端带 Happy Eyeballs（250ms 就
并行试下一个地址）所以毫不知情。这就是"别的工具能调通、这个程序卡死"
的根因。

做法
----
1. 解析后 **IPv4 排前面**，失败再按剩余地址回落；
2. 每次连接尝试都有上限（`CONNECT_TIMEOUT_SECONDS`），黑洞连接快速失败；
3. 换地址不影响 Host 头与 TLS SNI：证书仍按逻辑域名校验。

超时口径（不写死）
------------------
所有秒数都是**命名常量**，可以用环境变量覆盖，且互相之间存在**推导关系**：

* `CONNECT_TIMEOUT_SECONDS` —— 每次 TCP 连接尝试的上限。必须**小于**前端
  的心跳判死阈值（electron/sidecarClient.ts 的 `heartbeatTimeoutMs`），
  否则一次慢连接就会被误判成"侧车已死"。
* `HTTP_TIMEOUT_SECONDS` —— 出网请求总超时。必须**小于**前端的 RPC 单请求
  超时（`requestTimeoutMs`），否则界面只能拿到干巴巴的 RPC 超时，拿不到
  厂商的真实错误（401/403/404）。

改口径请改这里的常量或设环境变量，不要在调用点再写数字。
"""

from __future__ import annotations

import http.client
import os
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request

__all__ = [
    "CONNECT_TIMEOUT_SECONDS",
    "CONNECT_TIMEOUT_CAP",
    "HTTP_TIMEOUT_SECONDS",
    "SLOW_RPC_LOG_SECONDS",
    "connect_prefer_ipv4",
    "build_ipv4_first_opener",
    "describe_timeouts",
]


def _env_seconds(name: str, default: float) -> float:
    """环境变量覆盖（空值/坏值/非正数都退回默认，绝不抛）。"""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return float(default)
    try:
        value = float(raw)
    except ValueError:
        return float(default)
    return value if value > 0 else float(default)


#: 每次 TCP 连接尝试的上限（秒）。要小于前端心跳判死阈值。
CONNECT_TIMEOUT_SECONDS = _env_seconds("DESK_AGENT_CONNECT_TIMEOUT", 5.0)
#: 兼容旧引用名：逐地址连接上限就是 CONNECT_TIMEOUT_SECONDS。
CONNECT_TIMEOUT_CAP = CONNECT_TIMEOUT_SECONDS
#: 出网请求总超时（秒）。要小于前端 RPC 单请求超时。
HTTP_TIMEOUT_SECONDS = _env_seconds("DESK_AGENT_HTTP_TIMEOUT", 15.0)
#: 超过这个秒数算"慢请求"，必须留日志。
SLOW_RPC_LOG_SECONDS = _env_seconds("DESK_AGENT_SLOW_RPC_SECONDS", 2.0)
#: 响应体上限只由调用方按用途决定（例如取模型列表 capped read），
#: 本模块不预读响应体 —— 聊天是流式的，预读会破坏流式语义。


def describe_timeouts() -> str:
    """一行说明当前生效的超时口径（日志/诊断用）。"""
    return ("connect=%.1fs (per address, cap %.1fs) http=%.1fs slow-log>=%.1fs"
            % (CONNECT_TIMEOUT_SECONDS, CONNECT_TIMEOUT_CAP,
               HTTP_TIMEOUT_SECONDS, SLOW_RPC_LOG_SECONDS))


def _family_of(ip: str) -> int:
    return socket.AF_INET6 if ":" in ip else socket.AF_INET


def _default_resolver(host: str, port: int) -> list[str]:
    infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    out: list[str] = []
    for _fam, _type, _proto, _canon, sockaddr in infos:
        ip = str(sockaddr[0])
        if ip not in out:
            out.append(ip)
    return out


def _default_socket_factory(family: int, _addr) -> socket.socket:
    return socket.socket(family, socket.SOCK_STREAM)


def connect_prefer_ipv4(target, *, resolver=None, socket_factory=None,
                        connect_timeout=None):
    """连到 `target`（(host, port)），IPv4 优先，失败后按剩余地址回落。

    * `resolver(host, port) -> [ip, ...]`：默认真解析；测试可注入假解析。
    * `socket_factory(family, addr) -> socket`：默认真 socket；测试可注入。
    * `connect_timeout`：总的口径（会被 `CONNECT_TIMEOUT_CAP` 压住，
      所以调用方就算传 60 秒，逐地址尝试也不会超过上限）。

    成功返回已连接的 socket；全部失败抛最后一个连接异常（OSError 系）；
    解析失败让 `socket.gaierror` 直接向上冒。
    """
    host, port = target
    resolve = resolver or _default_resolver
    make_socket = socket_factory or _default_socket_factory
    per_try = min(float(connect_timeout or CONNECT_TIMEOUT_SECONDS),
                  float(CONNECT_TIMEOUT_CAP))

    addresses = list(resolve(host, port))          # gaierror 往上冒
    v4 = [ip for ip in addresses if _family_of(ip) == socket.AF_INET]
    rest = [ip for ip in addresses if _family_of(ip) != socket.AF_INET]
    ordered = v4 + rest                            # IPv4 优先
    if not ordered:
        raise socket.gaierror("no address for %s" % host)

    last_error: OSError | None = None
    for ip in ordered:
        sock = make_socket(_family_of(ip), (ip, port))
        try:
            sock.settimeout(per_try)
            sock.connect((ip, port))
            return sock
        except OSError as exc:                     # 超时/拒绝/不可达都回落
            last_error = exc
            try:
                sock.close()
            except Exception:                      # pragma: no cover - 防御
                pass
            continue
    raise last_error if last_error is not None else OSError(
        "could not connect to %s" % host)


class _PinnedTLSConnection(http.client.HTTPSConnection):
    """HTTPS 连接：TCP 已经按 IPv4 优先连好了，这里只补 TLS。

    `Host` 头与 `server_hostname`（SNI）都用逻辑域名，所以证书校验照旧按
    域名走 —— 换地址绝不会把校验绕过去。
    """

    def __init__(self, host: str, sock, timeout: float) -> None:
        self._ssl_context = ssl.create_default_context()
        super().__init__(host, 443, timeout=timeout,
                         context=self._ssl_context)
        self._pinned_sock = sock

    def connect(self) -> None:                     # noqa: D102 - 见类注释
        self.sock = self._ssl_context.wrap_socket(
            self._pinned_sock, server_hostname=self.host)


class _PinnedPlainConnection(http.client.HTTPConnection):
    """明文 HTTP 的同款做法（本机/内网端点用）。"""

    def __init__(self, host: str, port: int, sock, timeout: float) -> None:
        super().__init__(host, port, timeout=timeout)
        self._pinned_sock = sock

    def connect(self) -> None:                     # noqa: D102 - 见类注释
        self.sock = self._pinned_sock


class _LiveResponse:
    """活着读的响应对象 —— 与 urllib 的 HTTPResponse 同款接口。

    为什么不把响应体先读出来：聊天走 SSE 流式（``for raw in resp`` 逐行取
    data: 行），预读会把"流式"变成"憋完整段再吐"——长回答看起来卡住，还会
    撞超时。所以这里保留 http.client 的响应对象，只做薄包装：
    read / readline / 迭代 / 上下文管理器 / 关闭时连带关掉连接。

    Host 与 TLS SNI 都用逻辑域名，证书校验口径与 urllib 一致。
    """

    def __init__(self, conn, resp, url: str) -> None:
        self._conn = conn
        self._resp = resp
        self.url = url
        self.status = resp.status
        self.reason = resp.reason
        self.headers = resp.headers
        self._closed = False

    def read(self, amount: int = -1) -> bytes:
        return self._resp.read(amount)

    def readline(self, size: int = -1) -> bytes:
        return self._resp.readline(size)

    def __iter__(self):
        # HTTPResponse.__iter__ 逐行产出（SSE 就靠这个）
        return iter(self._resp)

    def info(self):
        return self.headers

    def getcode(self) -> int:
        return self.status

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for closer in (self._resp.close, self._conn.close):
            try:
                closer()
            except Exception:                      # pragma: no cover - 防御
                pass

    def __enter__(self):
        return self

    def __exit__(self, *_exc) -> bool:
        self.close()
        return False


def _exchange(host: str, port: int, sock, request, timeout: float,
              use_tls: bool):
    """在已连接的 socket 上发起请求，返回**活着的**响应对象（不预读）。"""
    # 连接阶段用的是被 CONNECT_TIMEOUT_CAP 压过的逐地址上限，而那个超时是 set 在
    # socket 上的 —— 已连接的 socket 又被连接对象直接复用，于是「5 秒」一路带进了
    # 后面的读写：模型 5 秒没吐出第一个字节就被判成读超时（真机 2026-10-14：
    # 请求 09:51:26 发出、09:51:31 报 TimeoutError，正好 5 秒；用户报的
    # 「特别容易超时」就是它，自动重试只是把它盖住了）。这里把超时改回请求自己的
    # 口径：连接依旧按 cap 封顶，读出网响应等多久由调用方 cfg.timeout_seconds 决定。
    try:
        sock.settimeout(float(timeout) or HTTP_TIMEOUT_SECONDS)
    except Exception:                              # pragma: no cover - 防御
        pass
    conn = (_PinnedTLSConnection(host, sock, timeout) if use_tls
            else _PinnedPlainConnection(host, port, sock, timeout))
    headers = {k: v for k, v in request.header_items()
               if k.lower() != "host"}
    url = getattr(request, "full_url", "") or ""
    try:
        conn.request(request.get_method() or "GET", request.selector or "/",
                     body=request.data, headers=headers)
        resp = conn.getresponse()
    except Exception:
        try:
            conn.close()
        except Exception:                          # pragma: no cover - 防御
            pass
        raise
    if resp.status >= 400:
        # 与 urllib 行为一致：4xx/5xx 抛 HTTPError，错误体挂在 fp 上
        try:
            raise urllib.error.HTTPError(url, resp.status, resp.reason,
                                         resp.headers, resp)
        finally:
            try:
                conn.close()
            except Exception:                      # pragma: no cover - 防御
                pass
    return _LiveResponse(conn, resp, url)


def build_ipv4_first_opener(connect_fn=None, http_open=None):
    """返回一个 urlopen 风格的 opener：`opener(request, timeout=...)`。

    * `connect_fn(host, port, timeout)`：默认走 `connect_prefer_ipv4`，测试可注入。
    * `http_open(request, timeout)`：可选的"真正发请求"实现（测试用），
      给了它就在连好的连接上调用它（连接由本函数负责关掉）。
    """
    connect = connect_fn or (
        lambda host, port, timeout: connect_prefer_ipv4(
            (host, port), connect_timeout=timeout))

    def opener(request, timeout: float | None = None):
        url = getattr(request, "full_url", None) or getattr(request, "url", "")
        parts = urllib.parse.urlsplit(url)
        host = parts.hostname or ""
        scheme = (parts.scheme or "https").lower()
        port = parts.port or (443 if scheme == "https" else 80)
        effective = float(timeout or HTTP_TIMEOUT_SECONDS)
        sock = connect(host, port, effective)
        if http_open is not None:
            try:
                return http_open(request, effective)
            finally:
                try:
                    sock.close()
                except Exception:                  # pragma: no cover - 防御
                    pass
        return _exchange(host, port, sock, request, effective,
                         scheme == "https")

    return opener