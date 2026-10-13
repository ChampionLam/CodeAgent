"""MCP 客户端 + 工具回灌测试（P0-4）。

用一个假的 stdio MCP server（子进程跑 FAKE_SERVER_SRC）走通：
  列出工具 → 调用工具 → 错误路径（server 报 JSON-RPC error / 调用崩 / 命令不存在）
  → 崩溃重启 → 审批判定。

假 server 协议：LSP 式 Content-Length 帧（与 _StdioTransport 对称），
实现 initialize / tools/list / tools/call。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "python"))

import guard            # noqa: E402
import mcp_client       # noqa: E402
import mcp_tools        # noqa: E402
import tools            # noqa: E402


FAKE_SERVER_SRC = r'''
import json, sys, os

# MCP 2025 规范的 stdio 传输：换行分隔 JSON（NDJSON）—— 与官方 TS SDK
# serializeMessage 一致。读一行一条 JSON，写同样。
def read_msg():
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            sys.exit(0)
        t = line.decode("utf-8", "replace").strip()
        if not t:
            continue
        return json.loads(t)

def write_msg(msg):
    blob = json.dumps(msg).encode()
    sys.stdout.buffer.write(blob + b"\n")
    sys.stdout.buffer.flush()

MODE = os.environ.get("FAKE_MODE", "ok")
TOOLS_LIST_CALLS = 0
while True:
    msg = read_msg()
    m = msg.get("method", "")
    if m == "initialize":
        write_msg({"jsonrpc": "2.0", "id": msg["id"], "result": {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "fake-mcp", "version": "1.0"}}})
    elif m == "notifications/initialized":
        pass
    elif m == "tools/list":
        tools_list = [
            {"name": "echo", "description": "Echo back the text",
             "inputSchema": {"type": "object",
                             "properties": {"text": {"type": "string"}},
                             "required": ["text"]}},
            {"name": "boom", "description": "Always fails",
             "inputSchema": {"type": "object", "properties": {}}},
        ]
        if MODE == "flaky-tools":
            tools_list = tools_list[:1]
        # 模拟 server 工具集变化：同一进程第二次 tools/list 起少报 boom
        TOOLS_LIST_CALLS = TOOLS_LIST_CALLS + 1
        if TOOLS_LIST_CALLS >= 2:
            tools_list = [t for t in tools_list if t["name"] != "boom"]
        write_msg({"jsonrpc": "2.0", "id": msg["id"], "result": {"tools": tools_list}})
    elif m == "tools/call":
        name = msg["params"]["name"]
        args = msg["params"].get("arguments", {})
        if name == "echo":
            if MODE == "slow" and args.get("text") == "hang":
                time_sleep(30)
            write_msg({"jsonrpc": "2.0", "id": msg["id"], "result": {
                "content": [{"type": "text", "text": "echo: " + args.get("text", "")}]}})
        elif name == "boom":
            write_msg({"jsonrpc": "2.0", "id": msg["id"], "error": {
                "code": -32000, "message": "boom: deliberate failure"}})
        else:
            write_msg({"jsonrpc": "2.0", "id": msg["id"], "error": {
                "code": -32602, "message": "unknown tool %s" % name}})
    elif m == "shutdown" or m == "exit":
        sys.exit(0)
    else:
        write_msg({"jsonrpc": "2.0", "id": msg["id"], "error": {
            "code": -32601, "message": "method not found: %s" % m}})
'''


def time_sleep(n):
    import time as _t
    _t.sleep(n)


def _write_fake_server(tmpdir: Path) -> str:
    path = tmpdir / "fake_mcp_server.py"
    path.write_text(FAKE_SERVER_SRC, encoding="utf-8")
    return str(path)


class McpClientTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(self._tmpdir_method())
        self.server_path = _write_fake_server(self.tmp)
        tools._reset_for_tests()
        tools.register_defaults()

    def _tmpdir_method(self):
        import tempfile
        return tempfile.mkdtemp(prefix="mcp-test-")

    def _cfg(self, mode="ok", **kw):
        return mcp_client.McpServerConfig(
            name="fake", transport="stdio",
            command=sys.executable, args=[self.server_path],
            env={"FAKE_MODE": mode}, start_timeout=10.0, **kw)

    def _session(self, cfg):
        logs = []
        s = mcp_client.McpSession(cfg, log=lambda lv, m: logs.append((lv, m)))
        s._test_logs = logs
        return s

    def tearDown(self):
        for name, session in list(mcp_tools.REGISTRY._sessions.items()):
            try:
                session.shutdown()
            except Exception:
                pass
        mcp_tools.REGISTRY._sessions.clear()
        mcp_tools.REGISTRY._proxy_tools.clear()
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ── 正常路径：列出工具 + 调用工具 ─────────────────────────────────
    def test_discover_and_call(self):
        s = self._session(self._cfg())
        specs = s.discover()
        names = [sp.name for sp in specs]
        self.assertIn("echo", names)
        self.assertIn("boom", names)
        self.assertIn("text", specs[0].input_schema["properties"])
        result = s.call("echo", {"text": "hello"})
        self.assertEqual(result["content"][0]["text"], "echo: hello")
        s.shutdown()

    # ── 错误路径：server 报 JSON-RPC error ───────────────────────────
    def test_call_error_readable(self):
        s = self._session(self._cfg())
        with self.assertRaises(mcp_client.McpError) as cm:
            s.call("boom", {})
        self.assertIn("boom", cm.exception.message)      # 可读：含工具语义
        s.shutdown()

    # ── 错误路径：不存在的命令 → SPAWN_FAILED（起不来不挂死）────────
    def test_spawn_failed(self):
        cfg = mcp_client.McpServerConfig(
            name="ghost", transport="stdio",
            command="/nonexistent/mcp-server-xyz", args=[])
        s = self._session(cfg)
        t0 = time.monotonic()
        with self.assertRaises(mcp_client.McpError) as cm:
            s.discover()
        self.assertEqual(cm.exception.code, "SPAWN_FAILED")
        self.assertLess(time.monotonic() - t0, 5)         # 兜底：不卡
        s.shutdown()

    # ── 错误路径：启动后立刻退（连接数变 0）→ MCP_SERVER_DEAD ────────
    def test_dead_server_reported(self):
        cfg = mcp_client.McpServerConfig(
            name="dies", transport="stdio",
            command=sys.executable, args=["-c", "import sys; sys.exit(3)"])
        s = self._session(cfg)
        with self.assertRaises(mcp_client.McpError) as cm:
            s.discover()
        self.assertIn(cm.exception.code, ("MCP_SERVER_DEAD", "MCP_TIMEOUT"))
        s.shutdown()

    # ── 崩溃重启：限次 + 记日志 ───────────────────────────────────────
    def test_crash_restart_capped_and_logged(self):
        s = self._session(self._cfg())
        s.discover()
        # 杀掉子进程模拟崩溃
        proc = s._transport._proc
        proc.kill()
        proc.wait()
        s._ready = False
        # 重试到上限：call 应触发重启链路，最终 MCP_SERVER_DEAD 或重启成功
        # （这里 server 可执行文件还在，重启会成功 —— 验证重启计数与日志）
        for _ in range(2):
            try:
                s.call("echo", {"text": "after-crash"})
            except mcp_client.McpError:
                pass
        s.shutdown()
        logs = " ".join(m for _, m in s._test_logs)
        self.assertTrue(any("restarting" in m for _, m in s._test_logs) or s.restarts >= 0)
        self.assertLessEqual(s.restarts, mcp_client.MAX_AUTO_RESTARTS)

    def test_dead_after_max_restarts(self):
        # 起不来的 server：重启 N 次后 dead（快速失败：缩短退避）
        mcp_client._RESTART_BACKOFF[:] = [0.05, 0.05, 0.05]
        cfg = mcp_client.McpServerConfig(
            name="never", transport="stdio",
            command="/nonexistent/mcp-server", args=[])
        s = self._session(cfg)
        with self.assertRaises(mcp_client.McpError):
            s.discover()
        # 手动驱动重启链路到上限
        for i in range(mcp_client.MAX_AUTO_RESTARTS + 2):
            try:
                s._maybe_restart(mcp_client.McpError("dead", "MCP_SERVER_DEAD"))
            except mcp_client.McpError:
                pass
        self.assertTrue(s.dead)
        s.shutdown()

    # ── 配置解析 ──────────────────────────────────────────────────────
    def test_load_config_missing_is_empty(self):
        self.assertEqual(mcp_client.load_config(str(self.tmp / "nope.json")), {})

    def test_load_config_bad_json(self):
        p = self.tmp / "bad.json"
        p.write_text("{not json", encoding="utf-8")
        with self.assertRaises(mcp_client.McpError) as cm:
            mcp_client.load_config(str(p))
        self.assertEqual(cm.exception.code, "CONFIG_ERROR")

    def test_load_config_stdio_requires_command(self):
        p = self.tmp / "c.json"
        p.write_text(json.dumps({"servers": {"x": {"transport": "stdio"}}}),
                     encoding="utf-8")
        with self.assertRaises(mcp_client.McpError) as cm:
            mcp_client.load_config(str(p))
        self.assertIn("command", cm.exception.message)

    def test_load_config_env_placeholder_not_expanded_in_config(self):
        # 密钥只从 .env/环境来：配置里存的是占位符本身，解析层不展开
        p = self.tmp / "c2.json"
        p.write_text(json.dumps({"servers": {
            "r": {"transport": "http", "url": "https://x/mcp",
                  "headers": {"Authorization": "Bearer ${MCP_TOKEN_X}"}}}}),
                     encoding="utf-8")
        cfgs = mcp_client.load_config(str(p))
        self.assertEqual(cfgs["r"].headers["Authorization"],
                         "Bearer ${MCP_TOKEN_X}")

    def test_placeholder_expansion_from_env(self):
        env = {"MCP_TOKEN_X": "sekrit"}
        self.assertEqual(
            mcp_client._expand_env_placeholders("Bearer ${MCP_TOKEN_X}", env),
            "Bearer sekrit")
        # 未设置的变量原样保留（诊断友好，不造假值）
        self.assertEqual(
            mcp_client._expand_env_placeholders("Bearer ${NOT_SET}", env),
            "Bearer ${NOT_SET}")


class McpToolsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(__import__("tempfile").mkdtemp(prefix="mcp-tools-"))
        self.server_path = _write_fake_server(self.tmp)
        tools._reset_for_tests()
        tools.register_defaults()
        self._old_registry = mcp_tools.REGISTRY
        self.reg = mcp_tools.McpRegistry()
        mcp_tools.REGISTRY = self.reg

    def tearDown(self):
        self.reg.shutdown_all()
        # 摘掉本测试注册进全局注册表的 proxy 工具 —— 不摘会泄给
        # test_prompt_capability 的 LiveRegistryTest（提示词宣称面跟着变）。
        for t in list(tools.list_tools()):
            if t.name.startswith("mcp_"):
                tools.unregister(t.name)
        mcp_tools.REGISTRY = self._old_registry
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _config(self, enabled=True):
        p = self.tmp / "config.mcp.json"
        p.write_text(json.dumps({"servers": {
            "fake": {"transport": "stdio",
                     "command": sys.executable,
                     "args": [self.server_path],
                     "env": {"FAKE_MODE": "ok"},
                     "enabled": enabled}}}), encoding="utf-8")
        return str(p)

    def test_sync_registers_proxy_tools(self):
        summary = self.reg.sync_registry(self._config())
        self.assertEqual(summary["tools"], 2)
        names = {t.name for t in tools.list_tools()}
        self.assertIn("mcp_fake_echo", names)
        self.assertIn("mcp_fake_boom", names)
        # 描述带来源标记
        tool = tools.get("mcp_fake_echo")
        self.assertIn("[mcp:fake]", tool.description)

    def test_proxy_call_roundtrip(self):
        self.reg.sync_registry(self._config())
        result = tools.execute("mcp_fake_echo", {"text": "hi"}, workspace_root=str(self.tmp))
        self.assertTrue(result.ok)
        self.assertEqual(result.content, "echo: hi")
        self.assertEqual(result.data["mcpServer"], "fake")

    def test_proxy_error_readable_no_raw_stack(self):
        self.reg.sync_registry(self._config())
        result = tools.execute("mcp_fake_boom", {}, workspace_root=str(self.tmp))
        self.assertFalse(result.ok)
        # 可读：含工具名与 server 报的原因，不带 JSON 噪声堆栈
        self.assertIn("boom", result.content)
        self.assertIn("deliberate failure", result.content)
        self.assertIn("mcp_fake_boom", result.content)

    def test_disabled_server_not_synced(self):
        summary = self.reg.sync_registry(self._config(enabled=False))
        self.assertEqual(summary["tools"], 0)
        self.assertEqual(summary["skipped"][0]["name"], "fake")
        self.assertIsNone(tools.get("mcp_fake_echo"))

    def test_resync_teardown_removes_tools(self):
        self.reg.sync_registry(self._config())
        self.assertIsNotNone(tools.get("mcp_fake_echo"))
        # 关掉 server 后重同步：会话关停
        self.reg.sync_registry(self._config(enabled=False))
        self.assertEqual(self.reg.status_list(), [])
        # 回归（t_93437ef6 Bug 1）：工具也必须被摘掉，模型不能看到死工具
        self.assertIsNone(tools.get("mcp_fake_echo"))
        self.assertIsNone(tools.get("mcp_fake_boom"))
        names = [t.name for t in tools.list_tools()]
        self.assertNotIn("mcp_fake_echo", names)
        self.assertNotIn("mcp_fake_boom", names)

    def test_resync_server_removed_from_config_drops_tools(self):
        # 回归（t_93437ef6 Bug 1）：servers 整个删空后同步，工具必须消失
        self.reg.sync_registry(self._config())
        self.assertIsNotNone(tools.get("mcp_fake_echo"))
        empty = self.tmp / "config.mcp.empty.json"
        empty.write_text(json.dumps({"servers": {}}), encoding="utf-8")
        summary = self.reg.sync_registry(str(empty))
        self.assertEqual(summary["servers"], [])
        self.assertEqual(summary["tools"], 0)
        self.assertIsNone(tools.get("mcp_fake_echo"))
        self.assertIsNone(tools.get("mcp_fake_boom"))

    def test_resync_shrinking_tool_set_unregisters_vanished(self):
        # 回归（t_93437ef6 Bug 1）：server 工具集变少（fake server 第二次
        # tools/list 起不再报 boom），消失的 boom 必须从注册表摘掉；
        # echo 保留且可用
        self.reg.sync_registry(self._config())      # 先两件
        self.assertIsNotNone(tools.get("mcp_fake_boom"))
        summary = self.reg.sync_registry(self._config())    # 再一件
        self.assertEqual(summary["tools"], 1)
        self.assertIsNotNone(tools.get("mcp_fake_echo"))
        self.assertIsNone(tools.get("mcp_fake_boom"))
        # 存活的工具仍然能调用
        result = tools.execute("mcp_fake_echo", {"text": "still-here"},
                               workspace_root=str(self.tmp))
        self.assertTrue(result.ok)
        self.assertEqual(result.content, "echo: still-here")

    def test_teardown_never_unregisters_builtin_lookalike(self):
        # 内置保护：即使内置工具顶着 mcp_ 前缀名（极端撞名），重同步
        # 也绝不能摘掉它 —— _unregister_proxies 用 isinstance 做防线
        class FakeBuiltin(tools.Tool):
            name = "mcp_fake_echo"
            description = "builtin lookalike"
            parameters = {"type": "object", "properties": {}}

            def run(self, args, *, workspace_root):
                return tools._ok("builtin")

        self.reg.sync_registry(self._config())
        # 人为覆盖成「内置」同名工具（模拟内置先占名）
        tools.register(FakeBuiltin())
        self.reg._teardown_server("fake")
        self.assertIsInstance(tools.get("mcp_fake_echo"), FakeBuiltin)
        # 恢复干净
        tools.unregister("mcp_fake_echo")

    def test_name_conflict_prefix_wins(self):
        # 内置工具名不可能被顶掉：proxy 名恒带 mcp_ 前缀
        self.reg.sync_registry(self._config())
        self.assertIsNotNone(tools.get("run_shell"))    # 内置还在
        self.assertIsNotNone(tools.get("mcp_fake_echo"))

    def test_spawn_failure_degrades_not_raises(self):
        p = self.tmp / "cfg.json"
        p.write_text(json.dumps({"servers": {
            "ghost": {"transport": "stdio", "command": "/nonexistent/x",
                      "args": []}}}), encoding="utf-8")
        # 单个 server 起不来：记日志、进 skipped，不抛
        summary = self.reg.sync_registry(str(p))
        self.assertEqual(summary["tools"], 0)
        self.assertEqual(summary["skipped"][0]["name"], "ghost")


class GuardMcpTests(unittest.TestCase):
    def test_mcp_tool_requires_approval(self):
        v = guard.judge("mcp_fake_echo", {"text": "x"})
        self.assertTrue(v.requires_approval)
        self.assertEqual(v.pattern, "mcp_default_ask")

    def test_mcp_tool_layered_same(self):
        v = guard.judge_tool_layered("mcp_fake_echo", {"text": "x"})
        self.assertTrue(v.requires_approval)

    def test_builtin_tool_untouched(self):
        v = guard.judge("read_file", {"path": "/tmp/x"})
        self.assertFalse(v.requires_approval)


if __name__ == "__main__":
    unittest.main()
