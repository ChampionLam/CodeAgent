"""Tests for the orchestration layer: system prompt assembly.

Frozen shape (design doc concepts/desktop-agent-app-design.md 281-285, 571-586):
    system = base role + skills list (name + description + when_to_use) + tools
The skill block's layout is asserted literally, because the model is told to call
read_skill off that list.
"""
from __future__ import annotations

import datetime
import os
import sys
import tempfile
import unittest
from dataclasses import dataclass

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "python"))

import prompt_build  # noqa: E402


@dataclass
class FakeSkill:
    name: str
    description: str
    when_to_use: str | None = None


@dataclass
class FakeTool:
    name: str
    description: str


class BaseRoleTest(unittest.TestCase):
    def test_base_role_carries_identity_workspace_and_clock(self):
        now = datetime.datetime(2026, 9, 23, 19, 40, 0)
        prompt = prompt_build.build(workspace="E:/ws", now=now)
        self.assertIn("CodeAgent", prompt)
        self.assertIn("E:/ws", prompt)
        self.assertIn("2026-09-23 19:40:00", prompt)
        self.assertIn("星期三", prompt)

    def test_base_role_keeps_the_anti_guessing_rules(self):
        prompt = prompt_build.build(workspace="E:/ws", now=datetime.datetime(2026, 1, 1))
        # Rule 1 (look before asserting) and the investigation ladder that replaced
        # the old "ask first" rule (see test_prompt_capability for the full set).
        self.assertIn("不许凭猜测下断言", prompt)
        self.assertIn("先看本地", prompt)
        self.assertIn("确实查不到才回头问用户", prompt)

    def test_empty_workspace_does_not_render_none(self):
        prompt = prompt_build.build(workspace="", now=datetime.datetime(2026, 1, 1))
        self.assertNotIn("None", prompt)
        self.assertIn("(未设置)", prompt)


class SkillsSectionTest(unittest.TestCase):
    def test_section_matches_the_frozen_layout(self):
        block = prompt_build.skills_section([
            FakeSkill("summarize-text", "把长文本压成要点", "用户给了一篇长文要总结时"),
            FakeSkill("another", "说明", None),
        ])
        lines = block.splitlines()
        self.assertEqual(lines[0], "## 可用 skills")
        # The instigation is a hard requirement, not an offer (see Hermes' live
        # prompt): scan first, load when in doubt, do not improvise steps.
        self.assertIn("回话前先扫一遍", lines[1])
        self.assertIn("倾向于读", lines[1])
        self.assertIn("不要凭记忆编流程", lines[1])
        self.assertEqual(lines[2], "- summarize-text: 把长文本压成要点 (when: 用户给了一篇长文要总结时)")
        self.assertEqual(lines[3], "- another: 说明")

    def test_no_skills_means_no_section_at_all(self):
        self.assertEqual(prompt_build.skills_section([]), "")
        self.assertNotIn("## 可用 skills", prompt_build.build(workspace="E:/ws", skills=[]))

    def test_description_is_collapsed_to_one_line(self):
        block = prompt_build.skills_section([FakeSkill("x", "第一行\n第二行\n第三行")])
        self.assertIn("- x: 第一行", block)
        self.assertNotIn("第二行", block)

    def test_skill_without_a_name_is_dropped(self):
        self.assertEqual(prompt_build.skills_section([FakeSkill("", "no name")]), "")


class ToolsSectionTest(unittest.TestCase):
    def test_tools_listed_by_name_and_first_line(self):
        block = prompt_build.tools_section([
            FakeTool("read_file", "Read a text file.\nReturns text."),
            FakeTool("run_shell", "Run a shell command."),
        ])
        self.assertEqual(block.splitlines()[0], "## 可用工具")
        self.assertIn("- read_file: Read a text file.", block)
        self.assertIn("- run_shell: Run a shell command.", block)
        self.assertNotIn("Returns text.", block)

    def test_no_tools_means_no_section(self):
        self.assertEqual(prompt_build.tools_section([]), "")


class AssemblyTest(unittest.TestCase):
    def test_sections_appear_in_order_base_skills_tools(self):
        now = datetime.datetime(2026, 9, 23, 12, 0, 0)
        prompt = prompt_build.build(workspace="E:/ws", skills=[FakeSkill("s", "d", None)],
                                    tools=[FakeTool("t", "d")], now=now)
        self.assertLess(prompt.index("你是 CodeAgent"), prompt.index("## 可用 skills"))
        self.assertLess(prompt.index("## 可用 skills"), prompt.index("## 可用工具"))

    def test_same_inputs_same_prompt(self):
        now = datetime.datetime(2026, 9, 23, 12, 0, 0)
        args = dict(workspace="E:/ws", skills=[FakeSkill("s", "d")], tools=[FakeTool("t", "d")], now=now)
        self.assertEqual(prompt_build.build(**args), prompt_build.build(**args))

    def test_tool_schemas_are_not_duplicated_into_the_prompt(self):
        # They travel in the OpenAI `tools` parameter; duplicating them here would
        # cost tokens on every single turn.
        block = prompt_build.tools_section([FakeTool("read_file", "Read a file.")])
        self.assertNotIn("parameters", block)
        self.assertNotIn("\"type\": \"object\"", block)


class SidecarHelperTest(unittest.TestCase):
    """`_system_prompt_for` is the sidecar's single entry into this module."""

    def test_helper_builds_a_prompt_with_the_base_role(self):
        import sidecar
        prompt = sidecar._system_prompt_for("E:/ws")
        self.assertIn("CodeAgent", prompt)
        self.assertIn("E:/ws", prompt)

    def test_helper_survives_a_broken_skills_registry(self):
        import sidecar
        old = os.environ.get("DESK_AGENT_SKILLS_DIR")
        os.environ["DESK_AGENT_SKILLS_DIR"] = os.path.join(tempfile.gettempdir(), "no-such-skills-dir-xyz")
        try:
            prompt = sidecar._system_prompt_for("E:/ws")   # must not raise
        finally:
            if old is None:
                os.environ.pop("DESK_AGENT_SKILLS_DIR", None)
            else:
                os.environ["DESK_AGENT_SKILLS_DIR"] = old
        self.assertIn("CodeAgent", prompt)


class EnvironmentSectionOrderTest(unittest.TestCase):
    """Volatile facts (clock, workspace) must come last.

    The clock changes every turn, so anything rendered after it can never be
    reused by a provider doing prefix caching. dsh makes the same choice by
    rendering runtime context after the first-party guidance.
    """

    def test_environment_is_the_final_section(self):
        prompt = prompt_build.build(
            workspace="E:/ws",
            skills=[FakeSkill("s", "d", None)],
            tools=[FakeTool("read_file", "Read a text file.")],
            now=datetime.datetime(2026, 9, 23, 19, 40, 0),
        )
        self.assertGreater(prompt.index("## 运行环境"), prompt.index("## 可用工具"))
        self.assertGreater(prompt.index("## 运行环境"), prompt.index("## 可用 skills"))
        tail = prompt[prompt.index("## 运行环境"):]
        self.assertIn("- 你干活时的当前目录：E:/ws", tail)
        self.assertTrue(tail.rstrip().splitlines()[-1].startswith("- 运行平台："),
                        repr(tail[-80:]))

    def test_stable_prefix_is_unchanged_when_only_the_clock_moves(self):
        """Two prompts one turn apart must share everything before the clock."""
        kw = dict(workspace="E:/ws", skills=[FakeSkill("s", "d", None)], tools=[FakeTool("t", "T.")])
        a = prompt_build.build(now=datetime.datetime(2026, 9, 23, 10, 0, 0), **kw)
        b = prompt_build.build(now=datetime.datetime(2026, 9, 23, 10, 0, 1), **kw)
        cut = a.index("## 运行环境")
        self.assertEqual(a[:cut], b[:cut])
        self.assertNotEqual(a, b)


class SkillsSectionBudgetTest(unittest.TestCase):
    """A long skill list degrades to names only (Hermes does the same)."""

    def _many(self, n, desc_len=400):
        return [FakeSkill("skill-%03d" % i, "D" * desc_len, None) for i in range(n)]

    def test_small_list_keeps_descriptions(self):
        block = prompt_build.skills_section(self._many(3))
        self.assertIn("skill-000: ", block)
        self.assertNotIn("只列名字", block)

    def test_long_list_degrades_to_names_only(self):
        block = prompt_build.skills_section(self._many(40))
        self.assertIn("只列名字", block)
        self.assertIn("- skill-000", block)
        self.assertNotIn("D" * 50, block)
        self.assertLessEqual(len(block), prompt_build.SKILLS_SECTION_CHAR_BUDGET + 400)

    def test_degradation_is_still_deterministic_and_lists_every_skill(self):
        block = prompt_build.skills_section(self._many(40))
        self.assertEqual(block, prompt_build.skills_section(self._many(40)))
        for i in (0, 17, 39):
            self.assertIn("- skill-%03d" % i, block)


if __name__ == "__main__":
    unittest.main()