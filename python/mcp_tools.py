"""MCP 工具回灌层（P0-4，design §2.3）。

把每个 enabled MCP server 的工具包成 McpProxyTool(Tool)，经 tools.register()
进现有注册表：
  name = "mcp_<server>_<tool>"   —— 前缀防撞现有内置工具名
  命名冲突：前缀后仍撞（同名 server+tool）按注册顺序后者覆盖（register 语义），
  但对**内置工具名**的撞名在 sync 时直接丢弃该 MCP 工具（不让外部 server
  顶掉内置能力）。

错误回灌纪律：McpError 的 message 本来就是人话（含 server 名与原因），
这里只做适度截断 + 去堆栈行（把 traceback/原始 stderr 尾巴剪掉再给模型）。
"""
from __future__ import annotations

import re
import threading

import mcp_client
import tools

#: 给模型看的错误正文上限（错误要可读，也不要拿几 KB stderr 灌上下文）
MAX_ERROR_CHARS = 800

_TOOL_NAME_RE = re.compile(r"[^A-Za-z0-9_]")
_STACK_LINE_RE = re.compile(r"^(Traceback|File \"|  \w+.*Error|\w+Error:)")

# 注册表重置钩子：tests 里 tools._reset_for_tests() 之后需要重新同步。
# sidecar 启动时 sync_registry() 一次；mcp.toggle / mcp.list RPC 也会调。


def _sanitize_tool_name(name: str) -> str:
    return _TOOL_NAME_RE.sub("_", name)[:64].strip("_")


def _clean_error_text(text: str) -> str:
    """错误可读化：剪长度 + 去堆栈行，不把原始堆栈直接塞给模型。"""
    lines = []
    for line in str(text or "").splitlines():
        if _STACK_LINE_RE.match(line.strip()):
            continue
        lines.append(line.rstrip())
    out = "\n".join(lines).strip()
    if len(out) > MAX_ERROR_CHARS:
        out = out[:MAX_ERROR_CHARS] + "…[truncated]"
    return out or "MCP server returned an empty error"


def _format_content(result: dict) -> str:
    """tools/call 的 content 块 → 模型可读正文。"""
    parts = []
    content = result.get("content") or []
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type", "text")
            if btype == "text":
                parts.append(str(block.get("text", "")))
            elif btype == "image":
                parts.append("[image: %s]" % block.get("mimeType", "unknown"))
            elif btype == "resource":
                res = block.get("resource") or {}
                parts.append("[resource: %s]" % (res.get("uri") or res.get("text", "")))
            else:
                parts.append("[%s block omitted]" % btype)
    text = "\n".join(p for p in parts if p)
    if result.get("isError"):
        text = "MCP tool reported an error:\n" + (text or "(no detail)")
    if not text:
        text = "(empty result)"
    return text


class McpProxyTool(tools.Tool):
    """一个 MCP 工具的代理。run() → tools._ok/_fail 包 McpSession.call。"""

    def __init__(self, server: mcp_client.McpSession, spec: mcp_client.McpToolSpec) -> None:
        self._server = server
        self._spec = spec
        self._proxy_name = "mcp_%s_%s" % (
            _sanitize_tool_name(server.cfg.name), _sanitize_tool_name(spec.name))
        # Tool 基类的 name/description/parameters 是类属性占位；实例上覆盖
        self.name = self._proxy_name
        self.description = ("[mcp:%s] %s" % (server.cfg.name, spec.description)
                            if spec.description else
                            "[mcp:%s] tool %r provided by an MCP server"
                            % (server.cfg.name, spec.name))
        self.parameters = spec.input_schema

    def run(self, args: dict, *, workspace_root: str) -> tools.ToolResult:
        if not isinstance(args, dict):
            return tools._fail("BAD_REQUEST", "args must be an object")
        try:
            result = self._server.call(self._spec.name, args)
        except mcp_client.McpError as exc:
            return tools._fail(exc.code, _clean_error_text(
                "MCP tool %s (server %s) failed: %s"
                % (self._proxy_name, self._server.cfg.name, exc.message)))
        except Exception as exc:                 # 传输层意外崩溃
            return tools._fail("MCP_INTERNAL", _clean_error_text(
                "MCP tool %s crashed: %s" % (self._proxy_name, exc)))
        return tools._ok(_format_content(result), mcpServer=self._server.cfg.name,
                         mcpTool=self._spec.name)


class McpRegistry:
    """server 名 → McpSession + 已注册的 proxy 工具名。线程安全。"""

    def __init__(self, log=None) -> None:
        self._log = log or (lambda level, msg: None)
        self._lock = threading.RLock()
        self._sessions: dict[str, mcp_client.McpSession] = {}
        self._proxy_tools: dict[str, list[str]] = {}   # server -> [tool names]
        self._config_path = ""

    @property
    def config_path(self) -> str:
        return self._config_path

    def status_list(self) -> list[dict]:
        with self._lock:
            return [s.status() for s in self._sessions.values()]

    def sync_registry(self, config_path: str) -> dict:
        """读配置 → 对每个 enabled server 建 session、discover、注册 proxy 工具。

        返回摘要（给日志/测试）：{"servers": [...], "tools": N, "skipped": [...]}。
        单个 server 失败只降级记日志，绝不挡其他 server，更不挡启动。
        """
        summary = {"servers": [], "tools": 0, "skipped": []}
        try:
            configs = mcp_client.load_config(config_path)
        except mcp_client.McpError as exc:
            self._log("warn", "mcp config invalid (%s): %s" % (exc.code, exc.message))
            return summary
        self._config_path = config_path

        with self._lock:
            # 关掉/消失的 server：摘工具 + 关会话（_teardown_server 里做）
            for name in list(self._sessions):
                if name not in configs or not configs[name].is_enabled:
                    self._teardown_server(name)
            for name, cfg in configs.items():
                if not cfg.is_enabled:
                    summary["skipped"].append({"name": name, "reason": "disabled"})
                    continue
                session = self._sessions.get(name)
                if session is None:
                    session = mcp_client.McpSession(cfg, log=self._log)
                    self._sessions[name] = session
                    self._proxy_tools[name] = []
                old_proxy_names = list(self._proxy_tools.get(name, []))
                try:
                    specs = session.discover()
                except mcp_client.McpError as exc:
                    # discover 失败：旧 proxy 指向的会话不可用（调用只会得到
                    # MCP_REMOTE 死工具），一并摘掉；会话保留等下次重试。
                    self._unregister_proxies(old_proxy_names)
                    self._proxy_tools[name] = []
                    summary["skipped"].append(
                        {"name": name, "reason": _clean_error_text(exc.message)})
                    self._log("warn", "mcp[%s] discover failed: %s" % (name, exc.message))
                    continue
                # 先摘旧 proxy 再注册新的（server 工具集可能变了）：
                # 集合差 = 消失的工具，必须 unregister，否则模型看到死工具。
                registered = []
                for spec in specs:
                    proxy = McpProxyTool(session, spec)
                    registered.append(proxy.name)
                    tools.register(proxy)
                self._unregister_proxies(
                    [n for n in old_proxy_names if n not in set(registered)])
                self._proxy_tools[name] = registered
                summary["servers"].append(session.status())
                summary["tools"] += len(registered)
                self._log("info", "mcp[%s]: %d tool(s) registered" % (name, len(registered)))
        return summary

    def _unregister_proxies(self, names: list[str]) -> None:
        """摘掉本 registry 注册的 proxy 工具（幂等，绝不误摘内置工具）。

        防线是 isinstance：只有当前注册表里该名字仍是 McpProxyTool 才摘 ——
        内置工具（哪怕撞了 mcp_ 前缀名）都不是 McpProxyTool，天然保护。
        """
        for name in names:
            existing = tools.get(name)
            if existing is None or not isinstance(existing, McpProxyTool):
                continue
            tools.unregister(name)
            self._log("info", "mcp: unregistered stale proxy tool %s" % name)

    def _teardown_server(self, name: str) -> None:
        # 先摘工具再关会话：摘掉后模型调不到，调用面立刻干净
        self._unregister_proxies(self._proxy_tools.get(name, []))
        session = self._sessions.pop(name, None)
        self._proxy_tools.pop(name, None)
        if session is not None:
            try:
                session.shutdown()
            except Exception as exc:            # pragma: no cover - 防御
                self._log("warn", "mcp[%s] shutdown failed: %s" % (name, exc))

    def shutdown_all(self) -> None:
        with self._lock:
            for name in list(self._sessions):
                self._teardown_server(name)


#: sidecar 装配点（进程级单例；测试里直接换）
REGISTRY = McpRegistry()


def sync_registry(config_path: str, log=None) -> dict:
    """模块级入口：sidecar 启动 / mcp.toggle 时调用。"""
    if log is not None:
        REGISTRY._log = log
    return REGISTRY.sync_registry(config_path)
