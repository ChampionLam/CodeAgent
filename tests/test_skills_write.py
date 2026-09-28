"""skill_manage 工具的单元测试（技能回写回路，2026-09-26 接线）。

覆盖三块：
  * 五条动作（create / patch / write_file / remove_file / delete）经工具层
    真正落盘并如实上报；
  * 内置技能拒写：错误码是 FORBIDDEN，只读根上不留任何痕迹；
  * 原子批：批里有一条坏操作 → 整批回滚，磁盘上不留半截。

纪律：所有写操作都落在 tempfile 隔离目录里（DESK_AGENT_SKILLS_DIR 重定向），
绝不碰真实的 ~/.desktop-agent/skills 和仓库里的 resources/skills。
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "python"))

import skills_registry  # noqa: E402
import tools  # noqa: E402


def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


class SkillManageTestBase(unittest.TestCase):
    """一套干净的注册表 + 两个隔离根（只读的 builtin + 可写的 user）。

    DESK_AGENT_SKILLS_DIR=builtin:user —— 按仓库约定，最后一项是可写用户根，
    其余按内置只读对待。两根都在 tempfile 里。
    """

    def setUp(self):
        tools.register_defaults()
        skills_registry.reset_for_tests()
        self._tmp = tempfile.TemporaryDirectory()
        self.builtin_root = os.path.join(self._tmp.name, "builtin")
        self.user_root = os.path.join(self._tmp.name, "user")
        os.makedirs(self.builtin_root)
        os.makedirs(self.user_root)
        self._saved = os.environ.get("DESK_AGENT_SKILLS_DIR")
        os.environ["DESK_AGENT_SKILLS_DIR"] = os.pathsep.join(
            [self.builtin_root, self.user_root])
        # 内置技能：只读根里放一个完整可扫描的 skill
        pinned = os.path.join(self.builtin_root, "pinned-skill")
        os.makedirs(pinned)
        with open(os.path.join(pinned, "SKILL.md"), "w", encoding="utf-8") as handle:
            handle.write("---\nname: pinned-skill\ndescription: 内置技能，不许写\n---\n\n内置正文。\n")
        skills_registry.reset_for_tests()

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("DESK_AGENT_SKILLS_DIR", None)
        else:
            os.environ["DESK_AGENT_SKILLS_DIR"] = self._saved
        skills_registry.reset_for_tests()
        self._tmp.cleanup()

    # ---- helpers -------------------------------------------------------
    def manage(self, operations: list):
        return tools.execute("skill_manage", {"operations": operations},
                             workspace_root=self.user_root)

    def manage_ok(self, operations: list):
        result = self.manage(operations)
        self.assertTrue(result.ok, "expected success, got %s: %s"
                        % (result.error_code, result.error_message))
        return result

    def create(self, name: str = "demo-skill") -> None:
        self.manage_ok([{"action": "create", "name": name,
                         "description": "演示用技能",
                         "body": "第一步：看一眼。\n第二步：动手。"}])


class TestRegistration(SkillManageTestBase):

    def test_tool_is_registered(self):
        names = {schema["function"]["name"] for schema in tools.schemas()}
        self.assertIn("skill_manage", names)

    def test_bad_request_on_missing_or_non_list_operations(self):
        for bad in (None, "nope", [], 5):
            result = tools.execute("skill_manage", {"operations": bad},
                                   workspace_root=self.user_root)
            self.assertFalse(result.ok, bad)
            self.assertEqual(result.error_code, "BAD_REQUEST")


class TestFiveActions(SkillManageTestBase):
    """五条动作各一条：走工具层 → 真落盘 → 结果如实上报。"""

    def test_create_writes_skill_md_under_the_user_root(self):
        result = self.manage_ok([{"action": "create", "name": "fresh-skill",
                                  "description": "新技能",
                                  "when_to_use": "需要新技能时",
                                  "body": "# 步骤\n\n1. 干活"}])
        skill_md = os.path.join(self.user_root, "fresh-skill", "SKILL.md")
        self.assertTrue(os.path.isfile(skill_md))
        text = _read(skill_md)
        self.assertIn("name: fresh-skill", text)
        self.assertIn("description: 新技能", text)
        self.assertIn("when_to_use: 需要新技能时", text)
        self.assertIn("1. 干活", text)
        # 内置根没被碰
        self.assertEqual(sorted(os.listdir(self.builtin_root)), ["pinned-skill"])
        # 结果如实上报，模型能看到落点
        self.assertIn("fresh-skill", result.content)
        self.assertEqual(result.data["applied"][0]["action"], "create")

    def test_patch_edits_skill_md_text(self):
        self.create()
        result = self.manage_ok([{"action": "patch", "name": "demo-skill",
                                  "old_string": "第二步：动手。",
                                  "new_string": "第二步：动手前先检查。"}])
        text = _read(os.path.join(self.user_root, "demo-skill", "SKILL.md"))
        self.assertIn("动手前先检查", text)
        self.assertNotIn("第二步：动手。\n", text)
        self.assertEqual(result.data["applied"][0]["action"], "patch")

    def test_write_file_lands_in_references_dir(self):
        self.create()
        result = self.manage_ok([{"action": "write_file", "name": "demo-skill",
                                  "rel_path": "references/api.md",
                                  "content": "# API 速查\nGET /x\n"}])
        target = os.path.join(self.user_root, "demo-skill", "references", "api.md")
        self.assertTrue(os.path.isfile(target))
        self.assertIn("GET /x", _read(target))
        self.assertEqual(result.data["applied"][0]["action"], "write_file")

    def test_remove_file_deletes_the_companion_file(self):
        self.create()
        self.manage_ok([{"action": "write_file", "name": "demo-skill",
                         "rel_path": "references/junk.md",
                         "content": "将删"}])
        result = self.manage_ok([{"action": "remove_file", "name": "demo-skill",
                                  "rel_path": "references/junk.md"}])
        target = os.path.join(self.user_root, "demo-skill", "references", "junk.md")
        self.assertFalse(os.path.exists(target))
        self.assertEqual(result.data["applied"][0]["action"], "remove_file")

    def test_delete_removes_the_whole_skill_directory(self):
        self.create()
        self.manage_ok([{"action": "write_file", "name": "demo-skill",
                         "rel_path": "references/api.md",
                         "content": "x"}])
        result = self.manage_ok([{"action": "delete", "name": "demo-skill"}])
        skill_dir = os.path.join(self.user_root, "demo-skill")
        self.assertFalse(os.path.exists(skill_dir))
        # 注册表视图同步：删完就查不到（用户根 mtime 变了，扫描缓存自动失效）
        self.assertIsNone(skills_registry.get_skill("demo-skill"))
        self.assertNotIn("demo-skill", [s.name for s in skills_registry.list_skills()])
        # 用户根里不再有这个技能目录（.skdel-* 待删归档是注册表的实现细节，不钉）
        self.assertNotIn("demo-skill", os.listdir(self.user_root))
        self.assertEqual(result.data["applied"][0]["action"], "delete")

    def test_two_actions_in_one_batch_share_the_call(self):
        result = self.manage_ok([
            {"action": "create", "name": "batch-skill",
             "description": "批内创建", "body": "起点。"},
            {"action": "patch", "name": "batch-skill",
             "old_string": "起点。", "new_string": "起点。终点。"},
        ])
        text = _read(os.path.join(self.user_root, "batch-skill", "SKILL.md"))
        self.assertIn("终点。", text)
        self.assertEqual(len(result.data["applied"]), 2)


class TestBuiltinReject(SkillManageTestBase):
    """内置技能拒写：FORBIDDEN + 不落盘。"""

    def test_patch_on_builtin_is_refused_without_touching_disk(self):
        before = _read(os.path.join(self.builtin_root, "pinned-skill", "SKILL.md"))
        result = self.manage([{"action": "patch", "name": "pinned-skill",
                               "old_string": "内置正文。",
                               "new_string": "被改掉。"}])
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "FORBIDDEN")
        self.assertIn("built-in", result.error_message or "")
        # 只读根一个字都没动；可写根也没长出遮蔽副本
        self.assertEqual(_read(os.path.join(self.builtin_root, "pinned-skill", "SKILL.md")),
                         before)
        self.assertEqual(os.listdir(self.user_root), [])

    def test_delete_on_builtin_is_refused_with_error_code(self):
        result = self.manage([{"action": "delete", "name": "pinned-skill"}])
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "FORBIDDEN")
        self.assertTrue(os.path.isdir(os.path.join(self.builtin_root, "pinned-skill")))
        self.assertEqual(os.listdir(self.user_root), [])

    def test_create_with_a_builtin_name_is_refused(self):
        result = self.manage([{"action": "create", "name": "pinned-skill",
                               "description": "冒名内置",
                               "body": "占位。"}])
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "FORBIDDEN")
        # 不许在用户根长出遮蔽副本
        self.assertEqual(os.listdir(self.user_root), [])


class TestAtomicRollback(SkillManageTestBase):
    """批里一条坏操作 → 整批回滚，磁盘不留半截。"""

    def test_one_bad_op_rolls_the_whole_batch_back(self):
        # 预置一个会被批里第二条改写的技能，验证「已成功的动作也被撤销」
        self.create("victim-skill")
        victim_md = os.path.join(self.user_root, "victim-skill", "SKILL.md")
        original = _read(victim_md)
        # 坏操作：patch 一个不存在的 old_string（NOT_FOUND，整批回滚）
        result = self.manage([
            {"action": "create", "name": "rolled-skill",
             "description": "本不该留下", "body": "半截。"},
            {"action": "patch", "name": "victim-skill",
             "old_string": "磁盘上根本没有这段话",
             "new_string": "改不动。"},
        ])
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "NOT_FOUND")
        self.assertIn("rolled back", result.error_message or "")
        # 回滚后：新建的技能目录消失、被改的技能恢复原文、可写根只剩 victim
        self.assertFalse(os.path.exists(os.path.join(self.user_root, "rolled-skill")))
        self.assertEqual(_read(victim_md), original)
        self.assertIn("victim-skill", os.listdir(self.user_root))
        # 内置根没被这条批碰过
        self.assertEqual(sorted(os.listdir(self.builtin_root)), ["pinned-skill"])

    def test_failed_batch_restores_a_skill_the_batch_itself_deleted(self):
        """回滚不是只撤销 create/patch：delete 搬走的目录也要原样搬回来。"""
        self.create("held-skill")
        held_md = os.path.join(self.user_root, "held-skill", "SKILL.md")
        original = _read(held_md)
        # 第一条 delete 成功（目录搬去临时处），第二条对不存在技能的 delete
        # 失败 → 逆序回滚：held-skill 必须整个回到磁盘上，内容一字不差
        result = self.manage([
            {"action": "delete", "name": "held-skill"},
            {"action": "delete", "name": "no-such-skill"},
        ])
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "NOT_FOUND")
        self.assertIn("rolled back", result.error_message or "")
        self.assertTrue(os.path.isfile(held_md))
        self.assertEqual(_read(held_md), original)
        self.assertIn("held-skill", os.listdir(self.user_root))
        # 回滚后注册表也看得到它（视图恢复）
        self.assertIsNotNone(skills_registry.get_skill("held-skill"))

    def test_unknown_action_as_first_op_lands_nothing(self):
        """形状校验（unknown action）在动作执行前拦下：坏的排第一条，啥都不落盘。"""
        self.create("kept-skill")
        result = self.manage([
            {"action": "explode", "name": "kept-skill"},
            {"action": "create", "name": "never-created",
             "description": "不该出现", "body": "x"},
        ])
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "BAD_REQUEST")
        self.assertIn("unknown action", result.error_message or "")
        # 第一条就拦下：既有的技能没被碰，后面的 create 也没跑
        self.assertTrue(os.path.isfile(os.path.join(self.user_root, "kept-skill", "SKILL.md")))
        self.assertFalse(os.path.exists(os.path.join(self.user_root, "never-created")))
        # 批被形状校验拦下时不宣称「已回滚」—— 什么都没发生过
        self.assertNotIn("rolled back", result.error_message or "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
