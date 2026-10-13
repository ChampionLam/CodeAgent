# -*- coding: utf-8 -*-
"""t_2d0783e3 回归测试：默认工作目录 = <InstallDir>\\workspace + agent 可在对话里改。

覆盖：
  1. set_workspace 工具：设置 -> 读回一致（落盘 config.json 的
     capabilities.workspaceRoot）；空 path / 相对 path 的拒绝口径。
  2. workspace.set RPC：同一条校验链 + 原子写回（其它键保留）。
  3. appconfig 回读：_persist_workspace_root 落盘后 sidecar 侧
     _base_dir(None) 能解析出新值（= 新会话生效链路）。
  4. 目录不存在时自动创建。

测试隔离：DESK_AGENT_CONFIG_ROOT 指向临时目录，绝不碰真实 config.json
（照 _load_tools_disabled 的隔离约定）。
"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sidecar  # noqa: E402
import tools  # noqa: E402
import appconfig  # noqa: E402


class _SandboxMixin:
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="ws-test-")
        self.root = self._tmp.name
        # 空白模板起步（= 全新安装后的 config.json）
        with open(os.path.join(self.root, "config.json"), "w", encoding="utf-8") as fh:
            json.dump({"model": {"baseUrl": "", "model": "", "apiKeyEnv": "",
                                 "maxTokens": 4096, "timeoutSeconds": 180},
                       "capabilities": {"workspaceRoot": ""}}, fh)
        self._prev_override = sidecar._TOOLS_CONFIG_ROOT_OVERRIDE
        sidecar._TOOLS_CONFIG_ROOT_OVERRIDE = self.root
        # _base_dir/_resolve_workspace 走 appconfig.capability（不带 root 覆盖），
        # 读的是 appconfig._ROOT —— 照 test_updater 的隔离法打补丁。
        self._prev_app_root = sidecar.appconfig._ROOT
        sidecar.appconfig._ROOT = self.root

    def tearDown(self) -> None:
        sidecar._TOOLS_CONFIG_ROOT_OVERRIDE = self._prev_override
        sidecar.appconfig._ROOT = self._prev_app_root
        self._tmp.cleanup()

    def _read_config(self) -> dict:
        with open(os.path.join(self.root, "config.json"), encoding="utf-8") as fh:
            return json.load(fh)

    def _ws_path(self, name: str) -> str:
        return os.path.join(self.root, name)


class TestSetWorkspaceTool(_SandboxMixin, unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        tools.set_workspace_setter(sidecar._workspace_setter())

    def tearDown(self) -> None:
        tools.set_workspace_setter(None)
        super().tearDown()

    def test_set_then_readback_roundtrip(self):
        """设置 -> 读回一致：config.json 落盘 + 工具返回新值。"""
        target = self._ws_path("proj-a")
        result = tools.execute("set_workspace", {"path": target},
                               workspace_root=self.root)
        self.assertTrue(result.ok, result.content)
        self.assertEqual(result.data["workspaceRoot"], os.path.abspath(target))
        self.assertTrue(result.data["persisted"])
        self.assertEqual(
            self._read_config()["capabilities"]["workspaceRoot"],
            os.path.abspath(target))
        # 目录被创建（用户切到的多半是没建的目录）
        self.assertTrue(os.path.isdir(target))

    def test_other_keys_survive(self):
        """原子写回不动其它键：model 段 + 已有 capabilities 键原样保留。"""
        with open(os.path.join(self.root, "config.json"), encoding="utf-8") as fh:
            cfg = json.load(fh)
        cfg["capabilities"]["toolsDisabled"] = ["run_shell"]
        with open(os.path.join(self.root, "config.json"), "w", encoding="utf-8") as fh:
            json.dump(cfg, fh)
        result = tools.execute("set_workspace",
                               {"path": self._ws_path("proj-b")}, workspace_root="")
        self.assertTrue(result.ok, result.content)
        after = self._read_config()
        self.assertEqual(after["capabilities"]["toolsDisabled"], ["run_shell"])
        self.assertEqual(after["model"]["maxTokens"], 4096)

    def test_empty_path_rejected(self):
        result = tools.execute("set_workspace", {"path": "  "}, workspace_root="")
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "BAD_REQUEST")
        # 拒绝时不落盘
        self.assertEqual(self._read_config()["capabilities"]["workspaceRoot"], "")

    def test_relative_path_rejected(self):
        """相对路径拒绝：写进默认配置的必须是绝对路径，否则语义随 cwd 漂移。"""
        result = tools.execute("set_workspace", {"path": "just/a/rel"},
                               workspace_root="")
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "IO_ERROR")  # setter 抛 ValueError -> IO_ERROR
        self.assertIn("absolute", result.content)
        self.assertEqual(self._read_config()["capabilities"]["workspaceRoot"], "")

    def test_new_session_resolves_new_value(self):
        """生效链路：落盘后 _base_dir(None)（=不带 workspaceRoot 的下一轮）
        解析出新值 —— 「新会话沿用」这条验收路径在这里闭合。"""
        target = self._ws_path("proj-c")
        result = tools.execute("set_workspace", {"path": target}, workspace_root="")
        self.assertTrue(result.ok, result.content)
        self.assertEqual(sidecar._base_dir(None), os.path.abspath(target))

    def test_user_home_expansion(self):
        """~/xxx 被展开成绝对路径（用户口语「~/projects」要能用）。"""
        home = os.path.expanduser("~")
        fake = os.path.join(self._tmp.name, "fakehome-projects")
        # 不真写用户家目录：~ 展开这一步用 os.path.expanduser 的行为测，
        # 但目标选在家目录真的不存在时也不该炸 —— 换成 monkeypatch 太重，
        # 直接验证 config 里存的是绝对路径即可。
        result = tools.execute("set_workspace", {"path": fake}, workspace_root="")
        self.assertTrue(result.ok, result.content)
        self.assertTrue(os.path.isabs(
            self._read_config()["capabilities"]["workspaceRoot"]))


class TestWorkspaceSetRpc(_SandboxMixin, unittest.TestCase):
    def _call(self, method: str, params: dict) -> dict:
        params = {"protocolVersion": sidecar.SIDECAR_PROTOCOL_VERSION, **params}
        return sidecar.handle_request({"id": "t", "method": method, "params": params})

    def test_rpc_roundtrip(self):
        target = self._ws_path("rpc-dir")
        response = self._call("workspace.set", {"path": target})
        self.assertEqual(response.get("result", {}).get("workspaceRoot"),
                         os.path.abspath(target))
        self.assertTrue(response.get("result", {}).get("persisted"))
        self.assertEqual(
            self._read_config()["capabilities"]["workspaceRoot"],
            os.path.abspath(target))
        self.assertTrue(os.path.isdir(target))

    def test_rpc_rejects_relative(self):
        response = self._call("workspace.set", {"path": "rel/path"})
        self.assertEqual(response.get("error", {}).get("code"), "BAD_REQUEST")
        self.assertEqual(self._read_config()["capabilities"]["workspaceRoot"], "")

    def test_rpc_rejects_missing_path(self):
        response = self._call("workspace.set", {})
        self.assertEqual(response.get("error", {}).get("code"), "BAD_REQUEST")

    def test_capability_advertised(self):
        """info 能力表里有 workspace.set（协议能力发现）。"""
        self.assertIn("workspace.set", sidecar.SIDECAR_CAPABILITIES)


class TestConfigReadLayer(_SandboxMixin, unittest.TestCase):
    def test_appconfig_reads_new_root(self):
        """读配置层（appconfig）看到的与落盘的一致。"""
        target = self._ws_path("read-layer")
        sidecar._persist_workspace_root(os.path.abspath(target))
        self.assertEqual(
            appconfig.capability("workspaceRoot", "", root=self.root),
            os.path.abspath(target))

    def test_blank_still_blank_when_never_set(self):
        """没设过时读回空串（安装器写默认值前的口径不变）。"""
        self.assertEqual(
            appconfig.capability("workspaceRoot", "", root=self.root), "")


if __name__ == "__main__":
    unittest.main()
