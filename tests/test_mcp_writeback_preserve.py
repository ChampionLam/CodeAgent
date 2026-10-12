"""MCP 写回保留未知段回归测试（t_6e40d247）。

断言：upsert / toggle / remove 三条写回路径都基于「落盘现读的完整配置」
做增量修改 —— model / capabilities / 用户手写的注释性键逐字节保留。

复现根因回归：文件存在但读失败/解析失败（外部脚本非原子改写中被读到
的半截 JSON）时，旧代码静默 raw={} → 写出只剩 mcp 段的 37 字节最小
文件。新代码必须 CONFIG_ERROR 拒写、原文一字不动。
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

#: 与真机观察一致的完整配置样例（含 mcp 段 → target 是主 config.json）
FULL = {
    "model": {
        "provider": "openai",
        "name": "gpt-4o-mini",
        "apiKey": "${MODEL_KEY}",
    },
    "capabilities": {
        "vision": {"provider": "local", "enabled": True},
        "toolsDisabled": [],
    },
    "_comment_zh": "用户手写的注释性键，写回不许动",
    "mcp": {
        "servers": {
            "e2e": {
                "transport": "stdio",
                "command": sys.executable,
                "args": ["@@SERVER_PATH@@"],
                "env": {"FAKE_MODE": "ok"},
            }
        }
    },
}


class McpWritebackPreserveTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="mcp-preserve-"))
        self.data_dir = self.root / "data"
        self.data_dir.mkdir()
        self.server_path = _write_fake_server(self.root)
        self._saved_cfg_root = sidecar._TOOLS_CONFIG_ROOT_OVERRIDE
        self._saved_data_dir = sidecar._DATA_DIR
        sidecar._TOOLS_CONFIG_ROOT_OVERRIDE = str(self.root)
        sidecar._DATA_DIR = str(self.data_dir)
        # 主 config.json 带 mcp 段 → _mcp_config_path() 解析到主文件
        self.config_path = self.root / "config.json"
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

    def write_full(self) -> None:
        doc = json.loads(json.dumps(FULL).replace(
            "@@SERVER_PATH@@", str(self.server_path)))
        self.config_path.write_text(
            json.dumps(doc, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8")
        self.rpc("mcp.reload")

    def read_raw(self) -> str:
        return self.config_path.read_text(encoding="utf-8")

    def read_doc(self) -> dict:
        return json.loads(self.read_raw())

    def assert_unrelated_keys_intact(self, before_raw: str) -> None:
        after = self.read_doc()
        self.assertEqual(after["model"], FULL["model"])
        self.assertEqual(after["capabilities"], FULL["capabilities"])
        self.assertEqual(after["_comment_zh"], FULL["_comment_zh"])
        # 序也保持：未知段仍在 mcp 段之前（写回没重排结构）
        keys = list(after.keys())
        self.assertLess(keys.index("mcp"), len(keys) - 1 + 1)
        before_doc = json.loads(before_raw)
        self.assertEqual([k for k in before_doc if k != "mcp"],
                         [k for k in after if k != "mcp"])
        del before_raw

    def fake_server(self, **extra) -> dict:
        base = {"transport": "stdio", "command": sys.executable,
                "args": [self.server_path], "env": {"FAKE_MODE": "ok"}}
        base.update(extra)
        return base

    # ── 主线：三条写回路径逐字节保留未知段 ────────────────────────
    def test_upsert_preserves_model_capabilities_and_unknown_key(self):
        self.write_full()
        before_raw = self.read_raw()
        res = self.rpc("mcp.upsert", {
            "name": "extra", "server": self.fake_server()})
        self.assertTrue(res["ok"], res)
        after = self.read_doc()
        self.assertIn("extra", after["mcp"]["servers"])
        self.assertIn("e2e", after["mcp"]["servers"])
        self.assert_unrelated_keys_intact(before_raw)

    def test_toggle_preserves_model_capabilities_and_unknown_key(self):
        self.write_full()
        before_raw = self.read_raw()
        res = self.rpc("mcp.toggle", {"name": "e2e", "enabled": False})
        self.assertTrue(res["ok"], res)
        after = self.read_doc()
        self.assertFalse(after["mcp"]["servers"]["e2e"]["enabled"])
        self.assert_unrelated_keys_intact(before_raw)
        # 再开回来同样保留
        before_raw = self.read_raw()
        res2 = self.rpc("mcp.toggle", {"name": "e2e", "enabled": True})
        self.assertTrue(res2["ok"], res2)
        self.assert_unrelated_keys_intact(before_raw)

    def test_remove_preserves_model_capabilities_and_unknown_key(self):
        self.write_full()
        before_raw = self.read_raw()
        res = self.rpc("mcp.remove", {"name": "e2e"})
        self.assertTrue(res["ok"], res)
        after = self.read_doc()
        self.assertNotIn("e2e", after["mcp"]["servers"])
        # 删空后 mcp.servers 是空 dict，但其余段全在
        self.assertEqual(after["mcp"], {"servers": {}})
        self.assert_unrelated_keys_intact(before_raw)

    # ── 复现根因：读失败必须拒写 ──────────────────────────────────
    def test_remove_on_corrupted_config_refuses_to_write(self):
        """37 字节事故的根因回归：半截 JSON → CONFIG_ERROR、原文不动。"""
        self.write_full()
        self.rpc("mcp.reload")
        # 模拟外部脚本非原子改写中的半截文件（删过 e2e 再写回时被撞上）
        broken = '{"mcp": {"servers": {"e2e": {"transport": "stdio",'
        self.config_path.write_text(broken, encoding="utf-8")
        for method, params in (
                ("mcp.remove", {"name": "e2e"}),
                ("mcp.upsert", {"name": "x", "server": self.fake_server()}),
                ("mcp.toggle", {"name": "e2e", "enabled": True})):
            res = self.rpc(method, params)
            self.assertFalse(res["ok"], (method, res))
            self.assertEqual(res["error"]["code"], "CONFIG_ERROR",
                             (method, res))
            # 原文一字不动（没有被覆盖成 {"mcp":{"servers":{}}}）
            self.assertEqual(self.read_raw(), broken)
        # 文件仍是那半截，没有产生 37 字节最小文件
        self.assertNotEqual(len(self.read_raw()), 37)

    def test_upsert_on_config_missing_allowed_to_bootstrap(self):
        """config.json 不存在 → 允许从最小结构新建（非损坏场景）。"""
        res = self.rpc("mcp.upsert", {
            "name": "new", "server": self.fake_server()})
        self.assertTrue(res["ok"], res)
        doc = json.loads((self.data_dir / "config.mcp.json").read_text(
            encoding="utf-8"))
        self.assertIn("new", doc["servers"])

    def test_top_level_non_object_refuses_to_write(self):
        """顶层不是对象（如 JSON 数组）同样拒写。"""
        self.config_path.write_text("[1, 2]", encoding="utf-8")
        before = self.read_raw()
        res = self.rpc("mcp.remove", {"name": "e2e"})
        self.assertFalse(res["ok"], res)
        self.assertEqual(res["error"]["code"], "CONFIG_ERROR")
        self.assertEqual(self.read_raw(), before)

    # ── 竞态硬拒写之外：损坏期间 describe 也要有明确报错 ──────────
    def test_describe_on_corrupted_config_reports_error(self):
        self.config_path.write_text("{oops", encoding="utf-8")
        res = self.rpc("mcp.describe")
        self.assertTrue(res["ok"], res)   # describe 不抛错给前端
        reasons = [s.get("reason", "") for s in res["result"]["skipped"]]
        self.assertTrue(any("not valid JSON" in r for r in reasons), reasons)


if __name__ == "__main__":
    unittest.main()
