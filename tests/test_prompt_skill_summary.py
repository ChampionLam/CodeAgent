"""技能自动总结的提示词口径：先问用户、点头才存（用户 2026-09-26 选定）。

这不是文案问题而是行为约束：它决定模型会不会自作主张往磁盘写技能。
所以既断言规则本身在提示词里，也断言能力清单那一行点明了「必须先问」——
能力清单只在相应工具真的注册了的时候才渲染，所以那条要带 tools 一起验。
"""
from __future__ import annotations

import datetime
import os
import sys
import unittest
from dataclasses import dataclass

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "python"))

import prompt_build  # noqa: E402


@dataclass
class FakeTool:
    name: str
    description: str


def _prompt(**kw) -> str:
    return prompt_build.build(workspace="E:/ws", now=datetime.datetime(2026, 9, 26, 12, 0, 0), **kw)


class SkillSummaryRuleTest(unittest.TestCase):
    def test_rule_asks_before_saving(self):
        """必须先问一句，不能直接写磁盘。"""
        self.assertIn("要不要把刚才这套流程存成技能", _prompt())

    def test_rule_saves_only_after_a_yes(self):
        p = _prompt()
        self.assertIn("用户点头了才用 skill_manage 存", p)
        self.assertIn("不置可否就当不要", p)

    def test_rule_excludes_one_off_work(self):
        """一次性的操作不该被存成技能，否则技能库会被垃圾填满。"""
        self.assertIn("一次性的操作不要存", _prompt())

    def test_capability_row_says_ask_first(self):
        rows = [row for row in prompt_build.CAPABILITY_CATALOG if "skill_manage" in row[1]]
        self.assertEqual(len(rows), 1, "skill_manage 应该只在能力清单里出现一次")
        flat = " ".join(str(part) for part in rows[0])
        self.assertIn("存新技能前必须先问用户", flat)

    def test_ask_first_reaches_the_prompt_when_tool_present(self):
        """工具在的时候，能力清单那一节真的会把「必须先问」带到模型眼前。"""
        p = _prompt(tools=[FakeTool("skill_manage", "Create, update, or delete skills")])
        self.assertIn("存新技能前必须先问用户", p)


if __name__ == "__main__":
    unittest.main()