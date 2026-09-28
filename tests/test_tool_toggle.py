"""工具开关 + skills.list / tools.setEnabled RPC 的测试。

隔离铁律：所有落盘操作都指向 tempfile 临时根（sidecar 的
DESK_AGENT_CONFIG_ROOT / skills_registry 的 DESK_AGENT_SKILLS_DIR），
绝不碰仓库的真实 config.json，也绝不碰 resources/skills 或
~/.desktop-agent/。

架构注：tools.py 是纯内存注入式（set_disabled/disabled），配置读写在
sidecar.py（_load_tools_disabled / _persist_tools_disabled）。所以
「落盘 + 重载生效」测的是 sidecar 的读回路径，不是 tools 的。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "python"))

import sidecar  # noqa: E402
import skills_registry  # noqa: E402
import tools  # noqa: E402

PV = {"protocolVersion": sidecar.SIDECAR_PROTOCOL_VERSION}

SKILL_MD = """---
name: {name}
description: {description}
when_to_use: {when}
version: 1.0.0
author: tester
---

# {name}

正文内容。
"""


def _rpc(method: str, params: dict | None = None) -> dict:
    merged = dict(PV)
    merged.update(params or {})
    return sidecar.handle_request(
        {"id": "t", "method": method, "params": merged})


class ToolToggleTestCase(unittest.TestCase):
    """共享脚手架：临时配置根 + 注册默认工具 + 每例后清状态。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="desk-agent-toggle-")
        self.root = self._tmp.name
        # 临时根里放一份最小 config.json，副作用是 appconfig.load 不会回落
        # 到仓库的 config.example.json（隔离两份真实配置来源）。
        with open(os.path.join(self.root, "config.json"), "w",
                  encoding="utf-8") as handle:
            json.dump({"capabilities": {"vision": False}}, handle)
        # sidecar 侧的工具开关配置根指向临时目录
        self._saved_root = sidecar._TOOLS_CONFIG_ROOT_OVERRIDE
        sidecar._TOOLS_CONFIG_ROOT_OVERRIDE = self.root
        self._reset_state()

    def tearDown(self) -> None:
        sidecar._TOOLS_CONFIG_ROOT_OVERRIDE = self._saved_root
        self._reset_state()
        self._tmp.cleanup()

    @staticmethod
    def _reset_state() -> None:
        tools.register_defaults()
        tools.set_disabled([])

    def _config(self) -> dict:
        with open(os.path.join(self.root, "config.json"),
                  encoding="utf-8") as handle:
            return json.load(handle)

    def _schema_names(self) -> set[str]:
        return {s["function"]["name"] for s in tools.schemas()}


class SchemasSkipDisabledTest(ToolToggleTestCase):
    def test_disable_removes_tool_from_schemas_and_enable_restores_it(self) -> None:
        self.assertIn("read_file", self._schema_names())
        tools.set_disabled(["read_file"])
        self.assertEqual(tools.disabled(), {"read_file"})
        self.assertNotIn("read_file", self._schema_names())
        self.assertIn("write_file", self._schema_names())
        # list_tools() 不受影响：界面仍能看到全部工具（含被禁的）。
        self.assertIn("read_file", {t.name for t in tools.list_tools()})
        tools.set_disabled([])
        self.assertIn("read_file", self._schema_names())
        self.assertEqual(tools.disabled(), set())

    def test_unknown_name_is_rejected_without_state_change(self) -> None:
        with self.assertRaises(tools.ToolToggleError) as ctx:
            tools.set_disabled(["no_such_tool"])
        self.assertEqual(ctx.exception.code, "UNKNOWN_TOOL")
        self.assertEqual(tools.disabled(), set())

    def test_set_disabled_dedupes_and_sorts(self) -> None:
        tools.set_disabled(["read_file", "run_shell", "read_file"])
        self.assertEqual(tools.disabled(), {"read_file", "run_shell"})


class PersistAndReloadTest(ToolToggleTestCase):
    """禁用表落盘 → 重载后仍生效（读写都在 sidecar 侧）。"""

    def test_persist_preserves_other_keys_and_survives_reload(self) -> None:
        sidecar._persist_tools_disabled(["edit_file", "read_file"])
        raw = self._config()
        # 其它键原样保留 + 禁用表排序落盘
        self.assertEqual(raw["capabilities"]["vision"], False)
        self.assertEqual(raw["capabilities"]["toolsDisabled"],
                         ["edit_file", "read_file"])
        # 「重新加载」：清掉内存注入，再走一遍启动读路径
        tools.set_disabled([])
        self.assertEqual(tools.disabled(), set())
        sidecar._load_tools_disabled()
        self.assertEqual(tools.disabled(), {"edit_file", "read_file"})
        self.assertNotIn("read_file", self._schema_names())
        self.assertNotIn("edit_file", self._schema_names())
        self.assertIn("run_shell", self._schema_names())

    def test_load_ignores_stale_names_missing_key_or_broken_config(self) -> None:
        # 配置里写一个注册表不认识的名字：整表仍加载，坏名字被丢弃
        raw = self._config()
        raw["capabilities"]["toolsDisabled"] = ["run_shell", "ghost_tool"]
        with open(os.path.join(self.root, "config.json"), "w",
                  encoding="utf-8") as handle:
            json.dump(raw, handle)
        sidecar._load_tools_disabled()
        self.assertEqual(tools.disabled(), {"run_shell"})
        # 键缺失 = 空表
        raw["capabilities"].pop("toolsDisabled")
        with open(os.path.join(self.root, "config.json"), "w",
                  encoding="utf-8") as handle:
            json.dump(raw, handle)
        sidecar._load_tools_disabled()
        self.assertEqual(tools.disabled(), set())
        # 坏 JSON = 空表（AppConfigError 被吞掉，不炸）
        with open(os.path.join(self.root, "config.json"), "w",
                  encoding="utf-8") as handle:
            handle.write("{not json")
        sidecar._load_tools_disabled()
        self.assertEqual(tools.disabled(), set())

    def test_persist_when_no_config_json_uses_example_as_base(self) -> None:
        # 临时根里删掉 config.json、只留 config.example.json：模板为起点
        os.unlink(os.path.join(self.root, "config.json"))
        with open(os.path.join(self.root, "config.example.json"), "w",
                  encoding="utf-8") as handle:
            json.dump({"capabilities": {"vision": True}}, handle)
        sidecar._persist_tools_disabled(["run_shell"])
        raw = self._config()                        # config.json 被新建
        self.assertEqual(raw["capabilities"]["vision"], True)   # 模板键保留
        self.assertEqual(raw["capabilities"]["toolsDisabled"], ["run_shell"])


class ToolToggleRpcTest(ToolToggleTestCase):
    def test_tools_list_entries_carry_enabled_flag(self) -> None:
        res = _rpc("tools.list")
        self.assertTrue(res["ok"], res)
        payload = res["result"]
        entries = payload["tools"]
        self.assertTrue(entries)
        for entry in entries:
            self.assertIn("enabled", entry)
            self.assertIsInstance(entry["enabled"], bool)
        by_name = {e["function"]["name"]: e for e in entries}
        self.assertEqual(len(by_name), len(entries))       # 名字唯一
        self.assertTrue(by_name["read_file"]["enabled"])
        self.assertEqual(payload["levels"]["read_file"], "")
        self.assertEqual(payload["canAlwaysAllow"][""], False)

    def test_set_enabled_round_trip_returns_same_shape_and_persists(self) -> None:
        # 关
        res = _rpc("tools.setEnabled", {"name": "run_shell", "enabled": False})
        self.assertTrue(res["ok"], res)
        first = res["result"]
        self.assertEqual(first["tools"], _rpc("tools.list")["result"]["tools"])
        by_name = {e["function"]["name"]: e for e in first["tools"]}
        self.assertFalse(by_name["run_shell"]["enabled"])
        self.assertIn("run_shell", first["levels"])
        # 落盘了
        self.assertEqual(self._config()["capabilities"]["toolsDisabled"],
                         ["run_shell"])
        # 模型的 schemas 也不再有它
        self.assertNotIn("run_shell", self._schema_names())
        # 开
        res = _rpc("tools.setEnabled", {"name": "run_shell", "enabled": True})
        self.assertTrue(res["ok"], res)
        by_name = {e["function"]["name"]: e for e in res["result"]["tools"]}
        self.assertTrue(by_name["run_shell"]["enabled"])
        self.assertEqual(self._config()["capabilities"]["toolsDisabled"], [])

    def test_set_enabled_rejects_unknown_tool_name(self) -> None:
        res = _rpc("tools.setEnabled", {"name": "not_a_tool", "enabled": False})
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"]["code"], "UNKNOWN_TOOL")
        # 内存和磁盘都没有被污染
        self.assertEqual(tools.disabled(), set())
        self.assertNotIn("toolsDisabled", self._config()["capabilities"])

    def test_set_enabled_validates_params(self) -> None:
        for bad in ({"enabled": True}, {"name": "read_file"},
                    {"name": "read_file", "enabled": "yes"}):
            res = _rpc("tools.setEnabled", bad)
            self.assertFalse(res["ok"], bad)
            self.assertEqual(res["error"]["code"], "BAD_REQUEST")
        self.assertEqual(tools.disabled(), set())          # 一个都没写进去

    def test_set_enabled_follows_startup_load(self) -> None:
        # 预置：启动加载注入了一个禁用项，setEnabled 的「开」要能摘掉它
        raw = self._config()
        raw["capabilities"]["toolsDisabled"] = ["run_shell"]
        with open(os.path.join(self.root, "config.json"), "w",
                  encoding="utf-8") as handle:
            json.dump(raw, handle)
        sidecar._load_tools_disabled()
        res = _rpc("tools.setEnabled", {"name": "run_shell", "enabled": True})
        self.assertTrue(res["ok"], res)
        self.assertEqual(self._config()["capabilities"]["toolsDisabled"], [])
        self.assertEqual(tools.disabled(), set())


class SkillsListRpcTest(ToolToggleTestCase):
    """skills.list 的形状：两个技能（一个 builtin 一个用户），断言全部字段。"""

    def setUp(self) -> None:
        super().setUp()
        skills_registry.reset_for_tests()
        self._saved_dirs = os.environ.get("DESK_AGENT_SKILLS_DIR")
        # 两个根：builtin 在前、user 在后 —— 正是生产布局（内置先扫、用户
        # 同名覆盖）。skills_registry 约定最后一个根可写、其余全按内置。
        self.builtin_root = os.path.join(self.root, "builtin-skills")
        self.user_root = os.path.join(self.root, "user-skills")
        for d in (self.builtin_root, self.user_root):
            os.makedirs(d, exist_ok=True)
        os.environ["DESK_AGENT_SKILLS_DIR"] = os.pathsep.join(
            [self.builtin_root, self.user_root])

    def tearDown(self) -> None:
        if self._saved_dirs is None:
            os.environ.pop("DESK_AGENT_SKILLS_DIR", None)
        else:
            os.environ["DESK_AGENT_SKILLS_DIR"] = self._saved_dirs
        skills_registry.reset_for_tests()
        super().tearDown()

    @staticmethod
    def _write_skill(root: str, name: str) -> None:
        skill_dir = os.path.join(root, name)
        os.makedirs(skill_dir, exist_ok=True)
        with open(os.path.join(skill_dir, "SKILL.md"), "w",
                  encoding="utf-8") as handle:
            handle.write(SKILL_MD.format(
                name=name, description="%s 的说明" % name, when="用户要%s时" % name))

    def test_skills_list_shape_and_builtin_flag(self) -> None:
        self._write_skill(self.builtin_root, "alpha-builtin")
        self._write_skill(self.user_root, "beta-user")
        res = _rpc("skills.list")
        self.assertTrue(res["ok"], res)
        payload = res["result"]
        self.assertIn("skills", payload)
        self.assertIn("skipped", payload)
        by_name = {s["name"]: s for s in payload["skills"]}
        self.assertEqual(set(by_name), {"alpha-builtin", "beta-user"})
        alpha, beta = by_name["alpha-builtin"], by_name["beta-user"]
        self.assertTrue(alpha["builtin"])
        self.assertFalse(beta["builtin"])
        for field in ("name", "description", "whenToUse", "version",
                      "author", "sourceDir", "builtin"):
            self.assertIn(field, alpha)
            self.assertIn(field, beta)
        self.assertEqual(alpha["description"], "alpha-builtin 的说明")
        self.assertEqual(alpha["whenToUse"], "用户要alpha-builtin时")
        self.assertEqual(alpha["version"], "1.0.0")
        self.assertEqual(alpha["author"], "tester")
        self.assertTrue(alpha["sourceDir"].startswith(self.builtin_root))
        self.assertTrue(beta["sourceDir"].startswith(self.user_root))

    def test_skills_list_reports_rejected_directories_as_skipped(self) -> None:
        # 一个没有 SKILL.md 的目录 → 走 skipped，而不是炸掉整个 RPC
        empty_dir = os.path.join(self.user_root, "no-skill-md")
        os.makedirs(empty_dir, exist_ok=True)
        res = _rpc("skills.list")
        self.assertTrue(res["ok"], res)
        payload = res["result"]
        self.assertEqual(payload["skills"], [])
        self.assertEqual(len(payload["skipped"]), 1)
        path, reason = payload["skipped"][0]
        self.assertTrue(path.endswith(os.path.join("no-skill-md", "SKILL.md")))
        self.assertIn("no SKILL.md", reason)


if __name__ == "__main__":
    unittest.main()
