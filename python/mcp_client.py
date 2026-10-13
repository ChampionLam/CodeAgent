"""MCP 客户端（P0-4，docs/design-agent-intelligence.md §2）。

职责边界（与 tools.py 同款纪律）：
  * 本模块只管「跟一个 MCP server 说话」：建传输、握手（initialize）、
    列工具（tools/list）、调工具（tools/call）、按需起停、崩溃兜底。
  * 不做权限裁决（guard 管）、不进注册表（mcp_tools 管）、不读配置文件
    的密钥语义（配置里只有 ${ENV} 占位符，本模块在展开时才从环境取，
    展开后的值不落盘、不进日志）。
  * 自实现 JSON-RPC 消息层，不引入 mcp SDK（依赖裁剪决策，见设计文档）。

MCP 消息层（面窄，只实现三方法）：
  stdio : LSP 式 Content-Length 分帧，一行 JSON 亦可（宽容解析）。
  http  : Streamable HTTP（POST JSON-RPC 到单一 endpoint）；旧 SSE-only
          server 不在 P0 范围（记录于设计文档风险节）。

生命周期纪律：
  * start() 带超时兜底 —— server 起不来必须报 MCP_TIMEOUT/SPAWN_FAILED，
    绝不能让 agent 主循环挂死。
  * 崩溃重启是有限次的（MAX_AUTO_RESTARTS），且每次都记结构化日志；
    超过次数就标记 dead，后续 call 返回 MCP_SERVER_DEAD（可读错误，
    不把子进程的原始堆栈直接塞给模型）。
"""
from __future__ import annotations

import json
import locale
import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field

import procflags

DEFAULT_START_TIMEOUT = 10.0        # 启动+initialize 兜底秒数
DEFAULT_CALL_TIMEOUT = 30.0         # 每次 tools/call 默认秒数
MAX_AUTO_RESTARTS = 3               # 崩溃自动重启次数上限（超限标 dead）
_RESTART_BACKOFF = [1.0, 2.0, 4.0]  # 重启退避（防 crash loop 烧 CPU）

_ENV_PLACEHOLDER = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class McpError(Exception):
    """带错误码的 MCP 失败（sidecar/工具层转成可读错误回灌模型）。"""

    def __init__(self, message: str, code: str = "MCP_FAILED") -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class McpToolSpec:
    name: str            # server 侧原始名
    description: str
    input_schema: dict


@dataclass
class McpServerConfig:
    name: str
    transport: str = "stdio"       # "stdio" | "http"
    command: str = ""
    args: list = field(default_factory=list)
    env: dict = field(default_factory=dict)
    url: str = ""
    headers: dict = field(default_factory=dict)
    disabled: bool = False
    enabled: bool = True             # 与 disabled 二选一表达，enabled 优先
    start_timeout: float = DEFAULT_START_TIMEOUT
    call_timeout: float = DEFAULT_CALL_TIMEOUT

    @property
    def is_enabled(self) -> bool:
        return self.enabled and not self.disabled


def _expand_env_placeholders(value: str, env: dict) -> str:
    """${VAR} 只在运行时从环境展开；未设置的变量原样保留（诊断友好）。"""
    def _sub(m):
        return env.get(m.group(1), m.group(0))
    return _ENV_PLACEHOLDER.sub(_sub, value)


def parse_server_config(name: str, raw: dict) -> McpServerConfig:
    """config.mcp.json 的 servers.<name> → McpServerConfig。字段错类型抛 McpError。"""
    if not isinstance(raw, dict):
        raise McpError("mcp server %s: config must be an object" % name, "CONFIG_ERROR")
    transport = str(raw.get("transport", "stdio")).strip().lower() or "stdio"
    if transport not in ("stdio", "http", "sse"):
        raise McpError("mcp server %s: unknown transport %r" % (name, transport), "CONFIG_ERROR")
    if transport == "sse":
        transport = "http"           # 2025 规范：SSE 并入 Streamable HTTP
    cfg = McpServerConfig(name=name, transport=transport)
    cfg.command = str(raw.get("command", "") or "")
    args = raw.get("args", [])
    if not isinstance(args, list):
        raise McpError("mcp server %s: 'args' must be a list" % name, "CONFIG_ERROR")
    cfg.args = [str(a) for a in args]
    env = raw.get("env", {})
    if not isinstance(env, dict):
        raise McpError("mcp server %s: 'env' must be an object" % name, "CONFIG_ERROR")
    cfg.env = {str(k): str(v) for k, v in env.items()}
    cfg.url = str(raw.get("url", "") or "")
    headers = raw.get("headers", {})
    if not isinstance(headers, dict):
        raise McpError("mcp server %s: 'headers' must be an object" % name, "CONFIG_ERROR")
    cfg.headers = {str(k): str(v) for k, v in headers.items()}
    cfg.disabled = bool(raw.get("disabled", False))
    cfg.enabled = bool(raw.get("enabled", True))
    for key, default in (("startTimeoutSeconds", DEFAULT_START_TIMEOUT),
                         ("callTimeoutSeconds", DEFAULT_CALL_TIMEOUT)):
        try:
            v = float(raw.get(key, default))
            if v > 0:
                attr = "start_timeout" if key == "startTimeoutSeconds" else "call_timeout"
                setattr(cfg, attr, v)
        except (TypeError, ValueError):
            pass
    return cfg


def load_config(path: str) -> dict[str, McpServerConfig]:
    """读 MCP 配置。两种形态都认：
      * config.mcp.json —— {"servers": {...}}（design §2.2，用户数据目录）
      * 主 config 的 mcp 段 —— {"mcp": {"servers": {...}}}（config.example.json 示例）
    不存在 → 空表（= 没配 MCP，不是错误）。
    JSON 坏 / 顶层不是对象 / servers 不是对象 → McpError(CONFIG_ERROR)。"""
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except json.JSONDecodeError as e:
        raise McpError("%s is not valid JSON: %s" % (os.path.basename(path), e),
                       "CONFIG_ERROR") from e
    if not isinstance(raw, dict):
        raise McpError("mcp config top level must be an object", "CONFIG_ERROR")
    servers = raw.get("servers")
    if servers is None and isinstance(raw.get("mcp"), dict):
        servers = raw["mcp"].get("servers")
    if servers is None:
        servers = {}
    if not isinstance(servers, dict):
        raise McpError("mcp config 'servers' must be an object", "CONFIG_ERROR")
    out: dict[str, McpServerConfig] = {}
    for name, entry in servers.items():
        if not isinstance(name, str) or not name.strip():
            continue
        cfg = parse_server_config(name.strip(), entry)
        # stdio 必须有 command；http 必须有 url —— 起不来的配置在解析期就报，
        # 别等到 spawn 才炸（配置错误是用户可修的，给出可读原因）。
        if cfg.transport == "stdio" and not cfg.command:
            raise McpError("mcp server %s: stdio transport requires 'command'" % name,
                           "CONFIG_ERROR")
        if cfg.transport == "http" and not cfg.url:
            raise McpError("mcp server %s: http transport requires 'url'" % name,
                           "CONFIG_ERROR")
        out[cfg.name] = cfg
    return out


class _StdioTransport:
    """LSP 式 Content-Length 分帧的 stdio JSON-RPC 传输。"""

    def __init__(self, cfg: McpServerConfig, log=None) -> None:
        self._cfg = cfg
        self._log = log
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._req_id = 0

    @property
    def alive(self) -> bool:
        proc = self._proc
        return proc is not None and proc.poll() is None

    def start(self) -> None:
        """spawn + 不阻塞（握手由 McpSession.start 带超时做）。"""
        env = dict(os.environ)
        for k, v in self._cfg.env.items():
            env[k] = _expand_env_placeholders(v, dict(os.environ))
        # MCP stdio 规范是 UTF-8；Windows 上不设此变量的子进程（尤其 python）
        # 会用区域编码（cp936）写 stdout，导致中文工具描述乱码。
        # 不覆盖用户已显式设置的值。
        env.setdefault("PYTHONIOENCODING", "utf-8")
        # Windows 兼容：node 官方 server 是 .cmd/.ps1 shim，shell=True 才找得到
        is_win = os.name == "nt"
        kwargs: dict = {}
        if is_win:
            kwargs["shell"] = True
        # Windows 上不弹控制台黑窗（CREATE_NO_WINDOW 与 shell=True 不冲突：
        # 仍走 cmd shim，只是不给它分配可见控制台；管道/编码不变）。
        kwargs = procflags.merge_no_window(kwargs)
        try:
            self._proc = subprocess.Popen(
                [self._cfg.command] + list(self._cfg.args),
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, env=env,
                **kwargs)
        except OSError as exc:
            raise McpError("cannot start mcp server %r: %s" % (self._cfg.command, exc),
                           "SPAWN_FAILED") from exc
        if self._log:
            self._log("info", "mcp[%s]: spawned pid=%s" % (self._cfg.name, self._proc.pid))

    @staticmethod
    def _decode_line(data: bytes) -> str:
        """UTF-8 优先（MCP stdio 规范）；若出现替换字符，回退区域编码再试，
        取替换字符更少的那个结果（兼容 Windows 上写 GBK 的 server）。"""
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            pass
        pref = locale.getpreferredencoding(False) or ""
        if pref and pref.lower().replace("-", "") != "utf8":
            try:
                alt = data.decode(pref)
                return alt
            except (UnicodeDecodeError, LookupError):
                pass
        return data.decode("utf-8", errors="replace")

    def _write_msg(self, payload: dict) -> None:
        """写一条消息。MCP 2025 规范的 stdio 传输是**换行分隔 JSON**（官方 TS
        SDK 的 serializeMessage = JSON.stringify + '\\n'）—— 不是 LSP 的
        Content-Length 帧（实测 server-everything 对帧式输入直接无视，
        initialize 永远等不到响应）。这里按规范写 NDJSON。"""
        assert self._proc and self._proc.stdin
        self._proc.stdin.write(json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n")
        self._proc.stdin.flush()

    def _read_msg(self) -> dict | None:
        """读一条消息。宽容两种帧：NDJSON（规范/官方 SDK）与 Content-Length
        （少数自实现 server）。空行跳过；EOF 返回 None。"""
        assert self._proc and self._proc.stdout
        while True:
            line = self._proc.stdout.readline()
            if not line:
                return None          # EOF：进程死了
            text = self._decode_line(line).strip()
            if not text:
                continue             # 空行：跳过，读下一条
            if text.lower().startswith("content-length:"):
                # 帧式 server：按头读 body
                length = 0
                while True:
                    inner = self._proc.stdout.readline()
                    if not inner:
                        return None
                    t = self._decode_line(inner).strip()
                    if not t:
                        break        # 帧头结束
                    k, _, v = t.partition(":")
                    if k.strip().lower() == "content-length":
                        length = int(v.strip() or "0")
                body = self._proc.stdout.read(length) if length else b""
                if not body:
                    continue
                return self._parse_body(body)
            # NDJSON 行
            parsed = self._parse_body(text)
            if parsed is None:
                continue             # banner / 非 JSON 行：不是消息，继续读
            return parsed

    def _parse_body(self, body) -> dict | None:
        if isinstance(body, bytes):
            body = self._decode_line(body)
        text = str(body).strip()
        if not text:
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            # 非 JSON 行（banner 之类）：当无消息处理，让调用方继续读。
            if text.startswith("{") or text.startswith("["):
                raise McpError("mcp server %s sent invalid JSON: %s"
                               % (self._cfg.name, exc), "MCP_PROTOCOL") from exc
            return None

    def request(self, method: str, params: dict | None = None, timeout: float = 30.0) -> dict:
        """发一个请求并等对应的响应（跳过期间夹杂的通知）。"""
        with self._lock:
            if not self.alive:
                raise McpError("mcp server %s is not running" % self._cfg.name,
                               "MCP_SERVER_DEAD")
            self._req_id += 1
            req = {"jsonrpc": "2.0", "id": self._req_id, "method": method}
            if params is not None:
                req["params"] = params
            self._write_msg(req)
            deadline = time.monotonic() + timeout
            # 裸 read 无 timeout：读线程循环「读到 id 匹配的响应或 EOF」为止，
            # 主线程按 deadline 兜底 —— server 挂死不能拖死 agent 主循环。
            import queue as _q
            box: _q.Queue = _q.Queue(maxsize=1)
            def _reader():
                try:
                    while True:
                        msg = self._read_msg()
                        if msg is None:               # EOF
                            box.put(("eof", None))
                            return
                        if msg.get("id") == self._req_id:
                            box.put(("msg", msg))
                            return                    # 夹杂的通知直接丢弃
                except Exception as exc:              # read 期崩溃
                    box.put(("err", exc))
            t = threading.Thread(target=_reader, daemon=True,
                                 name="mcp-read-%s" % self._cfg.name)
            t.start()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise McpError("mcp call %s.%s timed out" % (self._cfg.name, method),
                               "MCP_TIMEOUT")
            try:
                kind, val = box.get(timeout=remaining)
            except _q.Empty:
                raise McpError("mcp call %s.%s timed out after %.0fs"
                               % (self._cfg.name, method, timeout), "MCP_TIMEOUT")
            if kind == "err":
                raise McpError("mcp server %s read failed: %s" % (self._cfg.name, val),
                               "MCP_SERVER_DEAD")
            if kind == "eof":
                raise McpError("mcp server %s exited during %s" % (self._cfg.name, method),
                               "MCP_SERVER_DEAD")
            msg = val
            if "error" in msg:
                err = msg["error"] or {}
                raise McpError("mcp %s.%s: %s" % (self._cfg.name, method,
                                                  err.get("message", err)),
                               "MCP_REMOTE")
            return msg.get("result") or {}

    def shutdown(self) -> None:
        """优雅停（best-effort 通知）+ 强杀兜底。幂等。"""
        proc = self._proc
        if proc is None:
            return
        try:
            if proc.poll() is None and proc.stdin:
                # MCP 规范的 shutdown 通知；很多实现不回，写完就收
                self._write_msg({"jsonrpc": "2.0", "method": "shutdown"})
                self._write_msg({"jsonrpc": "2.0", "method": "exit"})
                try:
                    proc.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    pass
        except OSError:
            pass
        finally:
            if proc.poll() is None:
                proc.kill()
            self._proc = None

    def diagnostics(self) -> dict:
        proc = self._proc
        if proc is None or proc.poll() is None:
            return {}
        err = ""
        if proc.stderr:
            try:
                err = (proc.stderr.read() or b"")[-2000:].decode("utf-8", "replace")
            except OSError:
                pass
        return {"exitCode": proc.returncode, "stderrTail": err}


class _HttpTransport:
    """Streamable HTTP 传输（POST JSON-RPC）。无第三方依赖（urllib）。"""

    def __init__(self, cfg: McpServerConfig, log=None) -> None:
        self._cfg = cfg
        self._log = log
        self._req_id = 0
        self._initialized = False

    @property
    def alive(self) -> bool:
        return self._initialized

    def start(self) -> None:
        self._initialized = True    # 无进程可起；真正连通性在 initialize 时验证

    def request(self, method: str, params: dict | None = None, timeout: float = 30.0) -> dict:
        from urllib.request import Request, urlopen
        from urllib.error import HTTPError, URLError
        self._req_id += 1
        req = {"jsonrpc": "2.0", "id": self._req_id, "method": method}
        if params is not None:
            req["params"] = params
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        for k, v in self._cfg.headers.items():
            headers[k] = _expand_env_placeholders(v, dict(os.environ))
        data = json.dumps(req, ensure_ascii=False).encode("utf-8")
        r = Request(self._cfg.url, data=data, headers=headers, method="POST")
        try:
            with urlopen(r, timeout=timeout) as resp:
                ctype = resp.headers.get("Content-Type", "")
                raw = resp.read()
        except HTTPError as exc:
            detail = ""
            try:
                detail = (exc.read() or b"")[:500].decode("utf-8", "replace")
            except OSError:
                pass
            raise McpError("mcp http %s: HTTP %s %s" % (self._cfg.name, exc.code, detail),
                           "MCP_REMOTE") from exc
        except (URLError, OSError) as exc:
            # timeout 会以 socket.timeout（OSError 子类）冒出来
            if "timed out" in str(exc).lower():
                raise McpError("mcp http %s: request timed out" % self._cfg.name,
                               "MCP_TIMEOUT") from exc
            raise McpError("mcp http %s unreachable: %s" % (self._cfg.name, exc),
                           "MCP_REMOTE") from exc
        if "text/event-stream" in ctype:
            # Streamable HTTP 允许 SSE 拆响应：取第一个含 JSON-RPC response 的 data 行
            for line in raw.decode("utf-8", "replace").splitlines():
                line = line.strip()
                if line.startswith("data:"):
                    body = line[5:].strip()
                    try:
                        msg = json.loads(body)
                    except json.JSONDecodeError:
                        continue
                    if msg.get("id") == self._req_id:
                        if "error" in msg:
                            err = msg["error"] or {}
                            raise McpError("mcp %s.%s: %s" % (self._cfg.name, method,
                                                              err.get("message", err)),
                                           "MCP_REMOTE")
                        return msg.get("result") or {}
            raise McpError("mcp http %s: no response in SSE stream" % self._cfg.name,
                           "MCP_PROTOCOL")
        try:
            msg = json.loads(raw.decode("utf-8", "replace"))
        except json.JSONDecodeError as exc:
            raise McpError("mcp http %s sent invalid JSON: %s" % (self._cfg.name, exc),
                           "MCP_PROTOCOL") from exc
        if "error" in msg:
            err = msg["error"] or {}
            raise McpError("mcp %s.%s: %s" % (self._cfg.name, method, err.get("message", err)),
                           "MCP_REMOTE")
        return msg.get("result") or {}

    def shutdown(self) -> None:
        self._initialized = False

    def diagnostics(self) -> dict:
        return {}


class McpSession:
    """一个 MCP server 的会话：握手 → discover → call →（崩溃时）有限重启。"""

    def __init__(self, cfg: McpServerConfig, log=None) -> None:
        self.cfg = cfg
        self._log = log or (lambda level, msg: None)
        self._transport = (_StdioTransport(cfg, self._tlog)
                           if cfg.transport == "stdio" else _HttpTransport(cfg, self._tlog))
        self._ready = False
        self._restarts = 0
        self._dead = False
        self._server_info: dict = {}
        self._lock = threading.RLock()   # 串行化 start/call/shutdown

    def _tlog(self, level: str, msg: str) -> None:
        self._log(level, msg)

    # ── 状态（给 mcp.list RPC 用）────────────────────────────────────────
    @property
    def ready(self) -> bool:
        return self._ready

    @property
    def dead(self) -> bool:
        return self._dead

    @property
    def restarts(self) -> int:
        return self._restarts

    def status(self) -> dict:
        return {
            "name": self.cfg.name,
            "transport": self.cfg.transport,
            "ready": self._ready,
            "dead": self._dead,
            "restarts": self._restarts,
            "serverInfo": self._server_info,
        }

    # ── 生命周期 ─────────────────────────────────────────────────────────
    def start(self) -> None:
        """起传输 + initialize 握手。任何失败抛 McpError（带超时兜底）。"""
        with self._lock:
            if self._ready:
                return
            if self._dead:
                raise McpError("mcp server %s gave up after %d restart(s)"
                               % (self.cfg.name, self._restarts), "MCP_SERVER_DEAD")
            try:
                self._transport.start()
                result = self._transport.request(
                    "initialize",
                    {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {"name": "desk-agent-sidecar", "version": "0.3.0"},
                    },
                    timeout=self.cfg.start_timeout)
                # notifications/initialized（规范要求的第二步；http 下有些 server
                # 没它不认 tools/list —— 用 request 容错发法：通知无 id，直接跳过
                # 期望响应）。stdio 帧层只支持带响应的 request，这里通过 0 超时
                # 的 best-effort 发送处理：直接写不发等。
                self._notify_initialized()
                self._server_info = result.get("serverInfo") or {}
                self._ready = True
                self._log("info", "mcp[%s]: ready (%s)" % (
                    self.cfg.name, self._server_info.get("name", "unknown")))
            except McpError:
                self._transport.shutdown()
                raise

    def _notify_initialized(self) -> None:
        """发 notifications/initialized。失败不致命（部分 server 不需要）。"""
        try:
            if isinstance(self._transport, _StdioTransport):
                self._transport._write_msg(
                    {"jsonrpc": "2.0", "method": "notifications/initialized"})
            # http 传输：多数 Streamable HTTP server 不要求该通知，省一次往返
        except OSError:
            pass

    def ensure_started(self) -> None:
        if not self._ready:
            self.start()

    def _maybe_restart(self, exc: McpError) -> None:
        """stdio 进程崩了 → 有限次重启（记日志），超限标 dead。"""
        if self.cfg.transport != "stdio":
            return
        if exc.code not in ("MCP_SERVER_DEAD",):
            return
        with self._lock:
            self._ready = False
            if self._restarts >= MAX_AUTO_RESTARTS:
                self._dead = True
                diag = self._transport.diagnostics()
                self._log("error", "mcp[%s]: dead after %d restart(s) %s"
                          % (self.cfg.name, self._restarts,
                             ("(exit=%s)" % diag.get("exitCode")) if diag else ""))
                return
            backoff = _RESTART_BACKOFF[min(self._restarts, len(_RESTART_BACKOFF) - 1)]
            self._restarts += 1
            diag = self._transport.diagnostics()
            self._log("warn", "mcp[%s]: crashed %s, restarting (%d/%d, backoff %.0fs)"
                      % (self.cfg.name,
                         ("(exit=%s)" % diag.get("exitCode")) if diag else "",
                         self._restarts, MAX_AUTO_RESTARTS, backoff))
            self._transport.shutdown()
            time.sleep(backoff)
            try:
                self.start()
            except McpError as start_exc:
                if self._restarts >= MAX_AUTO_RESTARTS:
                    self._dead = True
                raise McpError("mcp server %s restart failed: %s"
                               % (self.cfg.name, start_exc.message),
                               start_exc.code) from start_exc

    # ── 面向 mcp_tools 的两个操作 ─────────────────────────────────────────
    def discover(self) -> list[McpToolSpec]:
        """tools/list → 规范化 spec 列表。"""
        self.ensure_started()
        result = self._transport.request("tools/list", {}, timeout=self.cfg.call_timeout)
        tools_raw = result.get("tools")
        if not isinstance(tools_raw, list):
            return []
        specs = []
        for t in tools_raw:
            if not isinstance(t, dict):
                continue
            name = t.get("name")
            if not isinstance(name, str) or not name.strip():
                continue
            schema = t.get("inputSchema")
            if not isinstance(schema, dict):
                schema = {"type": "object", "properties": {}}
            specs.append(McpToolSpec(
                name=name.strip(),
                description=str(t.get("description", "") or ""),
                input_schema=schema))
        return specs

    def call(self, tool: str, args: dict) -> dict:
        """tools/call → {"content": [...]}。崩溃走有限重启后重试一次。"""
        try:
            self.ensure_started()
            result = self._transport.request(
                "tools/call",
                {"name": tool, "arguments": args or {}},
                timeout=self.cfg.call_timeout)
            if not isinstance(result, dict):
                raise McpError("mcp %s: unexpected tools/call result" % self.cfg.name,
                               "MCP_PROTOCOL")
            return result
        except McpError as exc:
            if exc.code == "MCP_SERVER_DEAD":
                # 只对「进程死了」这一类做重启重试；超时/远端错误不重试
                self._maybe_restart(exc)
                result = self._transport.request(
                    "tools/call",
                    {"name": tool, "arguments": args or {}},
                    timeout=self.cfg.call_timeout)
                if not isinstance(result, dict):
                    raise McpError("mcp %s: unexpected tools/call result" % self.cfg.name,
                                   "MCP_PROTOCOL")
                return result
            raise

    def shutdown(self) -> None:
        with self._lock:
            self._ready = False
            self._transport.shutdown()
