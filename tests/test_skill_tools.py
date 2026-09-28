"""Tests for the two skill-facing tools wired into the orchestration layer.

read_skill is how the model walks from the one-line entry in the system prompt to
the full steps; list_skills is the same view the prompt is built from.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "python"))

import permissions  # noqa: E402
import skills_registry  # noqa: E402
import tools  # noqa: E402

SKILL = """---
name: demo
description: 演示用 skill
when_to_use: 需要演示时
---

# 演示

第一步：看一眼。
第二步：动手。
"""


class SkillToolTest(unittest.TestCase):
    def setUp(self):
        tools.register_defaults()
        skills_registry.reset_for_tests()
        self._tmp = tempfile.TemporaryDirectory()
        self._saved = os.environ.get("DESK_AGENT_SKILLS_DIR")
        os.environ["DESK_AGENT_SKILLS_DIR"] = self._tmp.name
        directory = os.path.join(self._tmp.name, "demo")
        os.makedirs(directory, exist_ok=True)
        with open(os.path.join(directory, "SKILL.md"), "w", encoding="utf-8") as handle:
            handle.write(SKILL)
        skills_registry.reset_for_tests()

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("DESK_AGENT_SKILLS_DIR", None)
        else:
            os.environ["DESK_AGENT_SKILLS_DIR"] = self._saved
        skills_registry.reset_for_tests()
        self._tmp.cleanup()

    def test_both_tools_are_registered(self):
        names = [schema["function"]["name"] for schema in tools.schemas()]
        self.assertIn("read_skill", names)
        self.assertIn("list_skills", names)

    def test_both_tools_never_ask_for_approval(self):
        """读技能与列技能都是只读且无副作用 —— 新口径下不需要审批（旧分级已取消）。"""
        import guard
        for name, args in (("read_skill", {"name": "demo"}), ("list_skills", {})):
            self.assertFalse(guard.judge(name, args).requires_approval, name)

    def test_read_skill_returns_the_body(self):
        result = tools.execute("read_skill", {"name": "demo"}, workspace_root=self._tmp.name)
        self.assertTrue(result.ok)
        self.assertIn("第一步", result.content)
        self.assertNotIn("name: demo", result.content)      # frontmatter stripped

    def test_read_skill_unknown_name_lists_what_is_installed(self):
        result = tools.execute("read_skill", {"name": "nope"}, workspace_root=self._tmp.name)
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "NOT_FOUND")
        self.assertIn("demo", result.error_message)

    def test_read_skill_rejects_empty_and_wrong_type_arguments(self):
        for bad in ({}, {"name": ""}, {"name": 3}, {"name": None}):
            result = tools.execute("read_skill", bad, workspace_root=self._tmp.name)
            self.assertFalse(result.ok, bad)
            self.assertEqual(result.error_code, "BAD_REQUEST")

    def test_read_skill_cannot_reach_outside_the_registry(self):
        # No path argument exists, so traversal has nowhere to go.
        result = tools.execute("read_skill", {"name": "../../../etc/passwd"}, workspace_root=self._tmp.name)
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "NOT_FOUND")

    def test_read_skill_survives_a_broken_registry(self):
        os.environ["DESK_AGENT_SKILLS_DIR"] = os.path.join(self._tmp.name, "gone")
        skills_registry.reset_for_tests()
        result = tools.execute("read_skill", {"name": "demo"}, workspace_root=self._tmp.name)
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "NOT_FOUND")

    def test_list_skills_returns_rows(self):
        result = tools.execute("list_skills", {}, workspace_root=self._tmp.name)
        self.assertTrue(result.ok)
        self.assertIn("- demo: 演示用 skill (when: 需要演示时)", result.content)

    def test_list_skills_with_nothing_installed(self):
        os.environ["DESK_AGENT_SKILLS_DIR"] = os.path.join(self._tmp.name, "empty")
        skills_registry.reset_for_tests()
        result = tools.execute("list_skills", {}, workspace_root=self._tmp.name)
        self.assertTrue(result.ok)
        self.assertIn("no skills installed", result.content)


if __name__ == "__main__":
    unittest.main()