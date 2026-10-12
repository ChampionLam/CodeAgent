"""mcp.describe / upsert / remove / reload RPC tests (t_d295723c).

纯本地零网络：用 tests/test_mcp.py 的假 stdio server 走通
  describe 全集（含停用项）
  upsert 新增 → 真 spawn → 真注册工具 → 配置里只有 ${ENV} 占位符
  upsert 明文密钥 → CONFIG_ERROR，配置不动
  remove → 配置里没了、进程停了、工具没了
  reload → 重同步幂等
  mcp.list / mcp.toggle 契约原样不动（回归）
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "python"))
sys.path.insert(0, os.path.join(REPO, "tests"))

import mcp_tools            # noqa: E402
import sidecar              # noqa: E402
import tools                # noqa: E402
from test_mcp import _write_fake_server  # noqa: E402


class McpAdminRpcTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="mcp-admin-"))
        self.data_dir = self.root / "data"
        self.data_dir.mkdir()
        self.server_path = _write_fake_server(self.root)
        # 隔离：配置根 + 数据目录都指向临时区，绝不碰真实文件
        self._saved_cfg_root = sidecar._TOOLS_CONFIG_ROOT_OVERRIDE
        self._saved_data_dir = sidecar._DATA_DIR
        sidecar._TOOLS_CONFIG_ROOT_OVERRIDE = str(self.root)
        sidecar._DATA_DIR = str(self.data_dir)
        # root 下没有 config.json → _mcp_config_path 落回 data/config.mcp.json
        self.config_path = self.data_dir / "config.mcp.json"
        tools._reset_for_tests()
        tools.register_defaults()
        self._saved_registry = mcp_tools.REGISTRY
        self.reg = mcp_tools.McpRegistry()
        mcp_tools.REGISTRY = self.reg
        mcp_tools.sync_registry.__globals__["REGISTRY"] = self.reg

    def tearDown(self):
        self.reg.shutdown_all()
        for t in list(tools.list_tools()):
            if t.name.startswith("mcp_"):
                tools.unregister(t.name)
        mcp_tools.REGISTRY = self._saved_registry
        mcp_tools.sync_registry.__globals__["REGISTRY"] = self._saved_registry
        sidecar._TOOLS_CONFIG_ROOT_OVERRIDE = self._saved_cfg_root
        sidecar._DATA_DIR = self._saved_data_dir
        import shutil
        shutil.rmtree(self.root, ignore_errors=True)

    # ── helpers ───────────────────────────────────────────────────
    def rpc(self, method, params=None):
        return sidecar.handle_request({
            "type": "req", "id": "t1", "method": method,
            "params": {"protocolVersion": 1, **(params or {})}})

    def write_config(self, servers: dict) -> None:
        self.config_path.write_text(
            json.dumps({"servers": servers}, ensure_ascii=False, indent=2),
            encoding="utf-8")

    def write_config_and_sync(self, servers: dict) -> None:
        """写配置并做一次重同步（sidecar 启动时本会做；测试进程里手动等价）。"""
        self.write_config(servers)
        self.rpc("mcp.reload")

    def read_config(self) -> dict:
        return json.loads(self.config_path.read_text(encoding="utf-8"))

    def fake_server(self, **extra) -> dict:
        base = {"transport": "stdio", "command": sys.executable,
                "args": [self.server_path], "env": {"FAKE_MODE": "ok"}}
        base.update(extra)
        return base

    # ── describe ──────────────────────────────────────────────────
    def test_describe_lists_configured_servers_including_disabled(self):
        self.write_config_and_sync({
            "fake": self.fake_server(),
            "off": self.fake_server(enabled=False),
        })
        res = self.rpc("mcp.describe")
        self.assertTrue(res["ok"], res)
        servers = {s["name"]: s for s in res["result"]["servers"]}
        self.assertIn("fake", servers)
        self.assertIn("off", servers)
        self.assertTrue(servers["fake"]["enabled"])
        self.assertFalse(servers["off"]["enabled"])
        self.assertEqual(servers["fake"]["transport"], "stdio")
        self.assertTrue(servers["fake"]["ready"])
        self.assertFalse(servers["fake"]["dead"])
        self.assertEqual(servers["fake"]["restarts"], 0)
        self.assertEqual(servers["fake"]["serverInfo"]["name"], "fake-mcp")
        self.assertEqual(servers["fake"]["toolCount"], 2)
        skipped = {s["name"] for s in res["result"]["skipped"]}
        self.assertEqual(skipped, {"off"})

    def test_describe_empty_config_is_empty_not_error(self):
        res = self.rpc("mcp.describe")
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["result"]["servers"], [])
        self.assertEqual(res["result"]["skipped"], [])

    # ── upsert ────────────────────────────────────────────────────
    def test_upsert_adds_server_spawns_and_registers_tools(self):
        self.write_config({})
        res = self.rpc("mcp.upsert", {
            "name": "new1",
            "server": self.fake_server(env={"FAKE_MODE": "ok",
                                            "TOKEN": "${FAKE_TOKEN}"})})
        self.assertTrue(res["ok"], res)
        # 真 spawn + 真注册
        self.assertIsNotNone(tools.get("mcp_new1_echo"))
        self.assertIsNotNone(tools.get("mcp_new1_boom"))
        result = tools.execute("mcp_new1_echo", {"text": "hi"},
                               workspace_root=str(self.root))
        self.assertTrue(result.ok)
        self.assertEqual(result.content, "echo: hi")
        # 配置落盘：占位符原样，没有明文值
        cfg = self.read_config()
        self.assertEqual(cfg["servers"]["new1"]["env"]["TOKEN"],
                         "${FAKE_TOKEN}")
        # describe 同步可见
        servers = {s["name"]: s for s in res["result"]["servers"]}
        self.assertTrue(servers["new1"]["ready"])
        self.assertEqual(servers["new1"]["toolCount"], 2)

    def test_upsert_rejects_plaintext_secret(self):
        self.write_config({})
        before = self.read_config()
        res = self.rpc("mcp.upsert", {
            "name": "leaky",
            "server": self.fake_server(env={
                "FAKE_MODE": "ok",
                "API_KEY": "sk-abcdefghijklmnopqrst"})})
        self.assertFalse(res["ok"], res)
        self.assertEqual(res["error"]["code"], "CONFIG_ERROR")
        # 配置一字不动
        self.assertEqual(self.read_config(), before)
        self.assertNotIn("leaky", self.read_config()["servers"])

    def test_upsert_rejects_bad_schema(self):
        res = self.rpc("mcp.upsert", {
            "name": "bad", "server": {"transport": "stdio"}})
        self.assertFalse(res["ok"], res)
        self.assertEqual(res["error"]["code"], "CONFIG_ERROR")
        # http 无 url 同样拒绝
        res2 = self.rpc("mcp.upsert", {
            "name": "bad2", "server": {"transport": "http"}})
        self.assertFalse(res2["ok"], res2)

    def test_upsert_updates_existing_server(self):
        self.write_config({"fake": self.fake_server()})
        res = self.rpc("mcp.upsert", {
            "name": "fake", "server": self.fake_server(enabled=False)})
        self.assertTrue(res["ok"], res)
        cfg = self.read_config()
        self.assertFalse(cfg["servers"]["fake"]["enabled"])
        # 停用 → 工具被摘
        self.assertIsNone(tools.get("mcp_fake_echo"))
        self.assertEqual(self.reg.status_list(), [])

    # ── remove ────────────────────────────────────────────────────
    def test_remove_drops_config_process_and_tools(self):
        self.write_config_and_sync({"fake": self.fake_server()})
        self.assertIsNotNone(tools.get("mcp_fake_echo"))
        session = self.reg._sessions.get("fake")
        proc_pid = session._transport._proc.pid if session else None
        res = self.rpc("mcp.remove", {"name": "fake"})
        self.assertTrue(res["ok"], res)
        # 配置里没了
        self.assertNotIn("fake", self.read_config()["servers"])
        # 进程停了（poll 返回非 None）
        if proc_pid is not None:
            import subprocess
            try:
                p = subprocess.run(["ps", "-p", str(proc_pid)],
                                   capture_output=True)
                # Linux 上孤儿 python 可能还在跑 shutdown 中；等一会
                import time
                for _ in range(20):
                    p = subprocess.run(["ps", "-p", str(proc_pid)],
                                       capture_output=True)
                    if p.returncode != 0:
                        break
                    time.sleep(0.1)
                self.assertNotEqual(p.returncode, 0,
                                    "进程 %s 还活着" % proc_pid)
            except FileNotFoundError:
                pass  # ps 不在的平台上跳过进程级断言
        # 工具没了
        self.assertIsNone(tools.get("mcp_fake_echo"))
        self.assertIsNone(tools.get("mcp_fake_boom"))
        # describe 里没了
        names = [s["name"] for s in res["result"]["servers"]]
        self.assertNotIn("fake", names)

    def test_remove_unknown_server_404(self):
        self.write_config({})
        res = self.rpc("mcp.remove", {"name": "nope"})
        self.assertFalse(res["ok"], res)
        self.assertEqual(res["error"]["code"], "NOT_FOUND")

    # ── reload ────────────────────────────────────────────────────
    def test_reload_resyncs_config(self):
        self.write_config({})
        res = self.rpc("mcp.reload")
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["result"]["servers"], [])
        # 直接改配置文件后 reload → 新 server 出现
        self.write_config({"fake": self.fake_server()})
        res2 = self.rpc("mcp.reload")
        self.assertTrue(res2["ok"], res2)
        names = [s["name"] for s in res2["result"]["servers"]]
        self.assertIn("fake", names)
        self.assertIsNotNone(tools.get("mcp_fake_echo"))

    # ── 契约不回退：mcp.list / mcp.toggle 原样 ─────────────────────
    def test_mcp_list_contract_unchanged(self):
        self.write_config_and_sync({"fake": self.fake_server()})
        res = self.rpc("mcp.list")
        self.assertTrue(res["ok"], res)
        entry = res["result"]["servers"][0]
        self.assertEqual(set(entry.keys()),
                         {"name", "transport", "ready", "dead",
                          "restarts", "serverInfo"})
        self.assertEqual(entry["name"], "fake")

    def test_mcp_toggle_contract_unchanged(self):
        self.write_config({"fake": self.fake_server()})
        res = self.rpc("mcp.toggle", {"name": "fake", "enabled": False})
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["result"]["servers"], [])
        res2 = self.rpc("mcp.toggle", {"name": "fake", "enabled": True})
        self.assertTrue(res2["ok"], res2)
        self.assertEqual(res2["result"]["servers"][0]["name"], "fake")
        self.assertTrue(res2["result"]["servers"][0]["ready"])

    def test_disabled_server_toggles_back_on(self):
        """真机验收 ① 的核心路径：关掉 → 工具消失；打开 → 重新 ready。"""
        self.write_config({"fake": self.fake_server()})
        self.rpc("mcp.toggle", {"name": "fake", "enabled": False})
        self.assertIsNone(tools.get("mcp_fake_echo"))
        self.rpc("mcp.toggle", {"name": "fake", "enabled": True})
        self.assertIsNotNone(tools.get("mcp_fake_echo"))
        result = tools.execute("mcp_fake_echo", {"text": "back"},
                               workspace_root=str(self.root))
        self.assertTrue(result.ok)
        self.assertEqual(result.content, "echo: back")


if __name__ == "__main__":
    unittest.main()
