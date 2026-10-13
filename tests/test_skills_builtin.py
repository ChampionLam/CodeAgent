"""Shipped-skills battery (2026-10-12, task t_3bb4debf).

Every directory under resources/skills/ must load through the real parser and
the real scan — this is the guard that a skill somebody adds tomorrow cannot
silently fail validation and vanish from the model's index. Also asserts the
registry degrades-to-logged (never raises) when a shipped skill is broken, and
that read_skill reaches the new skills by name.
"""
from __future__ import annotations

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "python"))

import skills_registry  # noqa: E402

BUILTIN = os.path.join(ROOT, "resources", "skills")


class ShippedSkillsTest(unittest.TestCase):
    """The whole built-in library must pass its own validation."""

    def setUp(self):
        skills_registry.reset_for_tests()
        self._saved = os.environ.get("DESK_AGENT_SKILLS_DIR")
        os.environ["DESK_AGENT_SKILLS_DIR"] = BUILTIN

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("DESK_AGENT_SKILLS_DIR", None)
        else:
            os.environ["DESK_AGENT_SKILLS_DIR"] = self._saved
        skills_registry.reset_for_tests()

    def test_every_shipped_directory_loads(self):
        dirs = sorted(d for d in os.listdir(BUILTIN)
                      if os.path.isdir(os.path.join(BUILTIN, d)))
        self.assertGreaterEqual(len(dirs), 10)
        loaded = skills_registry.scan()
        self.assertEqual(sorted(loaded), dirs, "every dir must become a skill")
        self.assertEqual(skills_registry.skipped(), [],
                         "no shipped skill may be rejected")

    def test_each_shipped_skill_has_trigger_body_and_size(self):
        for skill in skills_registry.list_skills():
            with self.subTest(skill=skill.name):
                self.assertTrue(skill.when_to_use, "trigger condition is required")
                self.assertTrue(skill.body.strip(), "body must not be empty")
                path = os.path.join(skill.source_dir, "SKILL.md")
                self.assertLessEqual(os.path.getsize(path),
                                     skills_registry.MAX_SKILL_FILE_BYTES)

    def test_validation_failures_are_logged_not_fatal(self):
        # A broken extra root must not stop the built-ins from loading.
        import tempfile
        with tempfile.TemporaryDirectory() as broken:
            with open(os.path.join(broken, "bogus.md"), "w", encoding="utf-8") as fh:
                fh.write("no frontmatter at all\n")
            os.makedirs(os.path.join(broken, "noname",), exist_ok=True)
            with open(os.path.join(broken, "noname", "SKILL.md"), "w",
                      encoding="utf-8") as fh:
                fh.write("---\nname: noname\ndescription: 缺触发条件\n---\n\n正文\n")
            os.environ["DESK_AGENT_SKILLS_DIR"] = os.pathsep.join([BUILTIN, broken])
            loaded = skills_registry.scan()
            self.assertIn("summarize-text", loaded)     # built-ins unaffected
            self.assertNotIn("noname", loaded)          # missing when_to_use
            reasons = {p for p, _r in skills_registry.skipped()}
            self.assertTrue(reasons)                    # and the failure is logged

    def test_read_skill_reaches_new_skills_by_name(self):
        sys.path.insert(0, os.path.join(ROOT, "python"))
        import tools
        for name in ("code-read-and-refactor", "run-and-debug",
                     "unknown-error-triage", "git-operations"):
            result = tools.execute("read_skill", {"name": name},
                                   workspace_root=ROOT)
            self.assertTrue(result.ok, name)
            self.assertIn("步骤", result.content)


if __name__ == "__main__":
    unittest.main()
