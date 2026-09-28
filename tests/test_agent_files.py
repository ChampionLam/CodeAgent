"""Identity and workspace-instruction files (the two layers Hermes has as
markdown and the desktop agent used to hard-code or lack entirely)."""
from __future__ import annotations

import os
import sys
import tempfile
import shutil
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "python"))

import agent_files  # noqa: E402
import prompt_build  # noqa: E402


class AgentFilesCase(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.mkdtemp(prefix="desk-agentfiles-")
        self.home = os.path.join(self.dir, "home")
        self.ws = os.path.join(self.dir, "workspace")
        os.makedirs(self.home)
        os.makedirs(self.ws)

    def tearDown(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)

    def _write(self, path, text):
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)


class IdentityTest(AgentFilesCase):
    def test_missing_file_means_no_identity(self) -> None:
        self.assertIsNone(agent_files.load_identity(self.home))

    def test_blank_file_means_no_identity(self) -> None:
        self._write(agent_files.identity_path(self.home), "   \n\n  ")
        self.assertIsNone(agent_files.load_identity(self.home))

    def test_identity_is_read_verbatim(self) -> None:
        self._write(agent_files.identity_path(self.home), "我是你的副驾驶，说话直接。\n")
        self.assertEqual(agent_files.load_identity(self.home), "我是你的副驾驶，说话直接。")

    def test_identity_is_capped_with_a_visible_marker(self) -> None:
        self._write(agent_files.identity_path(self.home), "甲" * (agent_files.IDENTITY_MAX_CHARS + 500))
        got = agent_files.load_identity(self.home)
        self.assertLessEqual(len(got), agent_files.IDENTITY_MAX_CHARS)
        self.assertTrue(got.endswith(agent_files.TRUNCATION_MARKER))


class InstructionsTest(AgentFilesCase):
    def test_agents_md_is_picked_up(self) -> None:
        self._write(os.path.join(self.ws, "AGENTS.md"), "本项目用 pnpm，别用 npm。")
        self.assertEqual(agent_files.load_instructions(self.ws), "本项目用 pnpm，别用 npm。")

    def test_workbuddy_md_is_the_second_choice(self) -> None:
        self._write(os.path.join(self.ws, "WORKBUDDY.md"), "备用名也认。")
        self.assertEqual(agent_files.load_instructions(self.ws), "备用名也认。")
        self._write(os.path.join(self.ws, "AGENTS.md"), "AGENTS 优先。")
        self.assertEqual(agent_files.load_instructions(self.ws), "AGENTS 优先。")

    def test_no_workspace_or_no_file_means_none(self) -> None:
        self.assertIsNone(agent_files.load_instructions(None))
        self.assertIsNone(agent_files.load_instructions(os.path.join(self.dir, "nope")))
        self.assertIsNone(agent_files.load_instructions(self.ws))

    def test_instructions_are_capped(self) -> None:
        self._write(os.path.join(self.ws, "AGENTS.md"), "乙" * (agent_files.INSTRUCTIONS_MAX_CHARS + 200))
        got = agent_files.load_instructions(self.ws)
        self.assertLessEqual(len(got), agent_files.INSTRUCTIONS_MAX_CHARS)
        self.assertTrue(got.endswith(agent_files.TRUNCATION_MARKER))


class SectionOrderTest(AgentFilesCase):
    """The runtime section must stay last: it changes every turn and would
    otherwise invalidate the provider's cached prefix."""

    class _Tool:
        def __init__(self, name, description):
            self.name = name
            self.description = description

    def _prompt(self, **kw):
        kw.setdefault("tools", [self._Tool("read_file", "Read a file")])
        return prompt_build.build(workspace="E:/ws", now=prompt_build._dt.datetime(2026, 9, 24, 10, 0), **kw)

    def test_sections_appear_in_a_stable_order(self) -> None:
        got = self._prompt(identity="我是副驾驶", instructions="用 pnpm")
        i_role = got.index(prompt_build.BASE_ROLE)
        i_identity = got.index(prompt_build.IDENTITY_TITLE)
        i_tools = got.index("## 可用工具")
        i_instr = got.index("## 项目指令（来自当前目录）")
        i_env = got.index("## 运行环境")
        self.assertLess(i_role, i_identity)
        self.assertLess(i_identity, i_instr)
        self.assertLess(i_instr, i_env)
        # The runtime section is the last one in the prompt (its own last line
        # is the platform, so "ends with the workspace path" would be wrong).
        self.assertEqual(got.rindex("## "), i_env)

    def test_absent_layers_add_nothing(self) -> None:
        bare = self._prompt()
        self.assertNotIn(prompt_build.IDENTITY_TITLE, bare)
        self.assertNotIn("## 项目指令（来自工作区）", bare)
        self.assertEqual(bare, self._prompt(identity="   ", instructions=None))

    def test_user_identity_does_not_erase_the_builtin_rules(self) -> None:
        got = self._prompt(identity="我是副驾驶")
        self.assertIn("## 工作方式", got)
        self.assertIn("先看再动", got)


if __name__ == "__main__":
    unittest.main()