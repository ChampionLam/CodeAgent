"""Hard rules live in the identity (SOUL) file -- not a layer of their own.

The agent may append a rule there; it is the only file the agent is allowed to
write, and it is rendered into every turn anyway. Three properties matter:
the user's own prose is never rewritten, nothing is evicted to make room, and a
saved rule actually reaches the prompt.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "python"))

import agent_files  # noqa: E402
import prompt_build  # noqa: E402
import tools  # noqa: E402


class RulesCase(unittest.TestCase):
    def setUp(self) -> None:
        self.home = tempfile.mkdtemp(prefix="desk-rules-")
        self._real_home = agent_files.agent_home
        agent_files.agent_home = lambda home=None: (home or self.home)
        tools.register_defaults()

    def tearDown(self) -> None:
        agent_files.agent_home = self._real_home
        shutil.rmtree(self.home, ignore_errors=True)

    def _identity_body(self) -> str:
        with open(agent_files.identity_path(), encoding="utf-8") as fh:
            return fh.read()


class RulesInIdentityFileTest(RulesCase):
    def test_rule_is_appended_to_a_fresh_identity_file(self) -> None:
        ok, reason = agent_files.append_rule("以后称呼我为示例用户")
        self.assertTrue(ok, reason)
        self.assertEqual(agent_files.load_identity(), "- 以后称呼我为示例用户")
        self.assertEqual(agent_files.list_rules(), ["以后称呼我为示例用户"])

    def test_user_prose_is_never_rewritten(self) -> None:
        with open(agent_files.identity_path(), "w", encoding="utf-8") as fh:
            fh.write("我是你的副驾驶，说话直接。\n\n## 我讨厌的\n- 废话\n")
        before = self._identity_body()
        agent_files.append_rule("以后称呼我为示例用户")
        after = self._identity_body()
        self.assertTrue(after.startswith(before.rstrip("\n")),
                        "existing text must stay untouched at the top")
        self.assertIn("- 以后称呼我为示例用户", after)

    def test_prose_lines_are_not_treated_as_rules(self) -> None:
        with open(agent_files.identity_path(), "w", encoding="utf-8") as fh:
            fh.write("我是你的副驾驶。\n- 回答用中文\n普通一句话\n")
        self.assertEqual(agent_files.list_rules(), ["回答用中文"])

    def test_duplicate_is_refused_not_appended_twice(self) -> None:
        agent_files.append_rule("回答用中文")
        ok, reason = agent_files.append_rule("  回答用中文  ")
        self.assertFalse(ok)
        self.assertEqual(reason, "already")
        self.assertEqual(agent_files.list_rules(), ["回答用中文"])

    def test_empty_is_refused(self) -> None:
        self.assertEqual(agent_files.append_rule("   "), (False, "empty"))

    def test_full_budget_refuses_instead_of_dropping_an_old_rule(self) -> None:
        first = "甲" * (agent_files.IDENTITY_MAX_CHARS // 2)
        second = "乙" * (agent_files.IDENTITY_MAX_CHARS // 2 - 60)
        self.assertTrue(agent_files.append_rule(first)[0])
        self.assertTrue(agent_files.append_rule(second)[0])
        ok, reason = agent_files.append_rule("丙" * 200)
        self.assertFalse(ok)
        self.assertEqual(reason, "full")
        self.assertEqual(len(agent_files.list_rules()), 2, "nothing may be evicted")

    def test_single_rule_over_the_whole_budget_is_refused(self) -> None:
        ok, reason = agent_files.append_rule("丁" * (agent_files.IDENTITY_MAX_CHARS + 10))
        self.assertFalse(ok)
        self.assertEqual(reason, "too_long")

    def test_remove_by_text_and_by_unique_fragment(self) -> None:
        agent_files.append_rule("以后称呼我为示例用户")
        agent_files.append_rule("回答用中文")
        self.assertTrue(agent_files.remove_rule("以后称呼我为示例用户")[0])
        self.assertEqual(agent_files.list_rules(), ["回答用中文"])
        self.assertTrue(agent_files.remove_rule("中文")[0])
        self.assertEqual(agent_files.list_rules(), [])

    def test_remove_keeps_non_rule_lines(self) -> None:
        with open(agent_files.identity_path(), "w", encoding="utf-8") as fh:
            fh.write("我是你的副驾驶。\n- 回答用中文\n")
        agent_files.remove_rule("回答用中文")
        self.assertIn("我是你的副驾驶。", self._identity_body())
        self.assertEqual(agent_files.list_rules(), [])

    def test_ambiguous_fragment_is_refused(self) -> None:
        agent_files.append_rule("回答用中文")
        agent_files.append_rule("回答别太长")
        ok, reason = agent_files.remove_rule("回答")
        self.assertFalse(ok)
        self.assertEqual(reason, "ambiguous")
        self.assertEqual(len(agent_files.list_rules()), 2)


class RulesToolTest(RulesCase):
    def _run(self, name, args):
        tool = next(t for t in tools.list_tools() if t.name == name)
        return tool.run(args, workspace_root=self.home)

    def test_save_rule_tool_writes_into_the_identity_file(self) -> None:
        res = self._run("save_rule", {"text": "以后称呼我为示例用户"})
        self.assertFalse(res.error_message)
        self.assertIn("以后称呼我为示例用户", self._identity_body())

    def test_save_rule_tool_reports_a_full_identity_file(self) -> None:
        agent_files.append_rule("甲" * (agent_files.IDENTITY_MAX_CHARS - 30))
        res = self._run("save_rule", {"text": "乙" * 100})
        self.assertTrue(res.error_message)
        self.assertIn("上限", res.error_message)

    def test_remove_rule_tool(self) -> None:
        agent_files.append_rule("以后称呼我为示例用户")
        res = self._run("remove_rule", {"text": "示例用户"})
        self.assertFalse(res.error_message)
        self.assertEqual(agent_files.list_rules(), [])

    def test_both_tools_reject_empty_input(self) -> None:
        for name in ("save_rule", "remove_rule"):
            self.assertTrue(self._run(name, {"text": "  "}).error_message, name)


class RulesInPromptTest(RulesCase):
    def _prompt(self, identity):
        return prompt_build.build(
            workspace="E:/ws", identity=identity,
            now=prompt_build._dt.datetime(2026, 9, 24, 10, 0))

    def test_hard_rules_render_inside_the_identity_section(self) -> None:
        got = self._prompt("- 以后称呼我为示例用户")
        self.assertIn(prompt_build.IDENTITY_TITLE, got)
        self.assertIn("- 以后称呼我为示例用户", got)
        self.assertIn("硬规", got)

    def test_identity_section_sits_after_the_role_and_before_the_runtime(self) -> None:
        got = self._prompt("我是副驾驶\n- 回答用中文")
        self.assertLess(got.index(prompt_build.BASE_ROLE), got.index(prompt_build.IDENTITY_TITLE))
        self.assertLess(got.index(prompt_build.IDENTITY_TITLE), got.index("## 运行环境"))
        self.assertTrue(got.rstrip().startswith(prompt_build.BASE_ROLE[:20]))

    def test_no_identity_file_means_no_section(self) -> None:
        self.assertNotIn(prompt_build.IDENTITY_TITLE, self._prompt(None))
        self.assertNotIn(prompt_build.IDENTITY_TITLE, self._prompt("   "))

    def test_rules_do_not_claim_to_override_the_permission_gate(self) -> None:
        self.assertIn("不能覆盖审批", self._prompt("- 回答用中文"))

    def test_builtin_working_rules_survive_a_user_identity(self) -> None:
        got = self._prompt("我是副驾驶")
        self.assertIn("## 工作方式", got)
        self.assertIn("先看再动", got)


if __name__ == "__main__":
    unittest.main()