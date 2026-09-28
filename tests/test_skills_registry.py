"""Tests for the skill registry (scan / frontmatter / body / guardrails)."""
from __future__ import annotations

import hashlib
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "python"))

import skills_registry  # noqa: E402

SKILL = """---
name: {name}
description: {description}
{extra}---

# 标题

第一步
第二步
"""


class RegistryTestCase(unittest.TestCase):
    def setUp(self):
        skills_registry.reset_for_tests()
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name
        self._saved = os.environ.get("DESK_AGENT_SKILLS_DIR")
        os.environ["DESK_AGENT_SKILLS_DIR"] = self.root

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("DESK_AGENT_SKILLS_DIR", None)
        else:
            os.environ["DESK_AGENT_SKILLS_DIR"] = self._saved
        skills_registry.reset_for_tests()
        self._tmp.cleanup()

    def write(self, dirname, text, filename="SKILL.md", root=None):
        directory = os.path.join(root or self.root, dirname)
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, filename)
        with open(path, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
        return path


class ParseTest(RegistryTestCase):
    def test_full_frontmatter(self):
        self.write("alpha", SKILL.format(name="alpha", description="说明", extra="when_to_use: 用户要总结时\n"))
        skills_registry.scan()
        skill = skills_registry.get_skill("alpha")
        self.assertIsNotNone(skill)
        self.assertEqual(skill.description, "说明")
        self.assertEqual(skill.when_to_use, "用户要总结时")
        self.assertIn("第一步", skill.body)
        self.assertNotIn("---", skill.body)
        self.assertNotIn("name:", skill.body)

    def test_when_to_use_is_optional(self):
        self.write("beta", SKILL.format(name="beta", description="没有触发条件", extra=""))
        skills_registry.scan()
        self.assertIsNone(skills_registry.get_skill("beta").when_to_use)

    def test_missing_description_is_skipped_with_a_reason(self):
        self.write("gamma", "---\nname: gamma\n---\n\n正文\n")
        skills_registry.scan()
        self.assertIsNone(skills_registry.get_skill("gamma"))
        reasons = skills_registry.skipped()
        self.assertEqual(len(reasons), 1)
        self.assertIn("description", reasons[0][1])

    def test_missing_frontmatter_is_skipped(self):
        self.write("delta", "# 只有正文，没有 frontmatter\n")
        skills_registry.scan()
        self.assertEqual(skills_registry.list_skills(), [])
        self.assertIn("frontmatter", skills_registry.skipped()[0][1])

    def test_quotes_are_stripped(self):
        self.write("q", '---\nname: "quoted"\ndescription: \'带引号\'\n---\n\n正文\n')
        skills_registry.scan()
        skill = skills_registry.get_skill("quoted")
        self.assertEqual(skill.description, "带引号")

    def test_crlf_is_handled(self):
        text = SKILL.format(name="crlf", description="换行符", extra="").replace("\n", "\r\n")
        self.write("crlf", text)
        skills_registry.scan()
        self.assertIsNotNone(skills_registry.get_skill("crlf"))

    def test_empty_body_is_skipped(self):
        self.write("empty", "---\nname: empty\ndescription: 没有正文\n---\n")
        skills_registry.scan()
        self.assertIsNone(skills_registry.get_skill("empty"))


class ScanTest(RegistryTestCase):
    def test_directory_is_a_skill_not_a_file(self):
        os.makedirs(os.path.join(self.root, "loose"), exist_ok=True)
        with open(os.path.join(self.root, "loose.md"), "w", encoding="utf-8") as handle:
            handle.write("---\nname: loose\ndescription: x\n---\n正文\n")
        skills_registry.scan()
        self.assertEqual(skills_registry.list_skills(), [])

    def test_skill_dir_without_skill_md_is_reported(self):
        os.makedirs(os.path.join(self.root, "noheart"), exist_ok=True)
        skills_registry.scan()
        self.assertIn("no SKILL.md", skills_registry.skipped()[0][1])

    def test_missing_root_does_not_crash(self):
        os.environ["DESK_AGENT_SKILLS_DIR"] = os.path.join(self.root, "does-not-exist")
        self.assertEqual(skills_registry.scan(), {})
        self.assertEqual(skills_registry.list_skills(), [])

    def test_later_root_overrides_earlier(self):
        builtin = os.path.join(self.root, "builtin")
        user = os.path.join(self.root, "user")
        self.write("dup", SKILL.format(name="dup", description="内置版", extra=""), root=builtin)
        self.write("dup", SKILL.format(name="dup", description="用户版", extra=""), root=user)
        os.environ["DESK_AGENT_SKILLS_DIR"] = os.pathsep.join([builtin, user])
        skills_registry.scan()
        self.assertEqual(skills_registry.get_skill("dup").description, "用户版")

    def test_sha256_matches_raw_bytes(self):
        path = self.write("sha", SKILL.format(name="sha", description="校验", extra=""))
        skills_registry.scan()
        with open(path, "rb") as handle:
            expected = hashlib.sha256(handle.read()).hexdigest()
        self.assertEqual(skills_registry.get_skill("sha").sha256, expected)

    def test_rescan_picks_up_edits(self):
        self.write("live", SKILL.format(name="live", description="第一版", extra=""))
        skills_registry.scan()
        first = skills_registry.get_skill("live").sha256
        self.write("live", SKILL.format(name="live", description="第二版", extra=""))
        skills_registry.reset_for_tests()
        skills_registry.scan()
        self.assertNotEqual(skills_registry.get_skill("live").sha256, first)

    def test_list_is_sorted_by_name(self):
        for name in ("zeta", "alpha", "mid"):
            self.write(name, SKILL.format(name=name, description="d", extra=""))
        skills_registry.scan()
        self.assertEqual([s.name for s in skills_registry.list_skills()], ["alpha", "mid", "zeta"])

    def test_read_body_returns_none_for_unknown(self):
        skills_registry.scan()
        self.assertIsNone(skills_registry.read_body("nope"))

    def test_default_dirs_order_and_env_override(self):
        os.environ.pop("DESK_AGENT_SKILLS_DIR", None)
        dirs = skills_registry.default_skill_dirs()
        self.assertEqual(len(dirs), 2)
        self.assertIn(os.path.join("resources", "skills"), dirs[0])
        os.environ["DESK_AGENT_SKILLS_DIR"] = "/a" + os.pathsep + "/b"
        self.assertEqual(skills_registry.scan.__module__ and skills_registry._target_dirs(None), ["/a", "/b"])


class BuiltinSkillTest(unittest.TestCase):
    """The repo ships one real skill; it has to survive its own parser."""

    def setUp(self):
        skills_registry.reset_for_tests()

    def test_shipped_summarize_text_parses(self):
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "resources", "skills", "summarize-text", "SKILL.md")
        with open(path, encoding="utf-8") as handle:
            meta, body = skills_registry.parse_skill_md(handle.read())
        self.assertEqual(meta["name"], "summarize-text")
        self.assertTrue(meta["description"])
        self.assertTrue(meta["when_to_use"])
        self.assertIn("步骤", body)


class GuardrailTest(unittest.TestCase):
    """The design doc promises: a skill dir can never smuggle in executable code."""

    def setUp(self):
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "python", "skills_registry.py")
        with open(path, encoding="utf-8") as handle:
            self.source = handle.read()

    def test_registry_never_references_a_script_directory(self):
        self.assertNotIn("scripts", self.source)

    def test_registry_opens_files_read_only(self):
        for forbidden in ('"w"', "'w'", '"a"', "'a'", '"wb"', "'wb'"):
            self.assertNotIn("open(" + forbidden, self.source)
        self.assertIn('open(path, "r"', self.source)

    def test_registry_only_ever_opens_skill_md(self):
        self.assertIn('SKILL_FILENAME = "SKILL.md"', self.source)
        self.assertNotIn("listdir", self.source.replace("os.listdir(root)", ""))


if __name__ == "__main__":
    unittest.main()