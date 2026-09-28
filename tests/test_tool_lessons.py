"""工具失败经验库单测：记账 / 去重 / 自我纠正 / 提示词注入 / 落盘原子性。"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "python"))

import tool_lessons as tl  # noqa: E402


class TargetOfTest(unittest.TestCase):
    def test_url_uses_host(self):
        self.assertEqual(tl.target_of("web_fetch", {"url": "https://xyq.17173.com/a/b"}), "xyq.17173.com")
        self.assertEqual(tl.target_of("web_fetch", {"url": "http://gamewac.com/x"}), "gamewac.com")

    def test_path_uses_file_name(self):
        self.assertEqual(tl.target_of("read_file", {"path": r"G:\baidu_dl\通天河副本.pdf"}), "通天河副本.pdf")
        self.assertEqual(tl.target_of("read_file", {"path": "/tmp/a/b.md"}), "b.md")

    def test_command_uses_head(self):
        self.assertEqual(tl.target_of("run_shell", {"command": "pdftotext a.pdf -"}), "pdftotext")

    def test_query_and_unknown(self):
        self.assertEqual(tl.target_of("web_search", {"query": "通天河 攻略"}), "通天河 攻略")
        self.assertEqual(tl.target_of("whatever", {}), "")


class LessonStoreTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="lessons-")
        self.path = os.path.join(self.dir, "tool_lessons.json")
        self.store = tl.LessonStore(self.path)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_record_dedupes_same_target(self):
        self.store.record("web_fetch", {"url": "https://a.com/1"}, "HTTP_ERROR", "500 boom")
        self.store.record("web_fetch", {"url": "https://a.com/2"}, "HTTP_ERROR", "500 boom")
        entries = self.store.entries()
        self.assertEqual(len(entries), 1, "同 host 的不同 URL 归一条经验")
        self.assertEqual(entries[0]["count"], 2)
        self.assertEqual(entries[0]["code"], "HTTP_ERROR")

    def test_different_targets_are_separate(self):
        self.store.record("web_fetch", {"url": "https://a.com/1"}, "HTTP_ERROR", "x")
        self.store.record("web_fetch", {"url": "https://b.com/1"}, "HTTP_ERROR", "x")
        self.assertEqual(len(self.store.entries()), 2)

    def test_persists_and_reloads(self):
        self.store.record("read_file", {"path": r"C:\x.pdf"}, "EXTRACT", "no text layer")
        again = tl.LessonStore(self.path)
        self.assertEqual(len(again.entries()), 1)
        self.assertEqual(again.entries()[0]["tool"], "read_file")

    def test_success_decays_and_removes(self):
        self.store.record("web_fetch", {"url": "https://a.com/1"}, "E", "x")
        self.assertTrue(self.store.note_success("web_fetch", {"url": "https://a.com/9"}))
        self.assertEqual(self.store.entries(), [], "成功一次就把这条经验撤掉（自我纠正）")
        self.assertFalse(self.store.note_success("web_fetch", {"url": "https://a.com/1"}))

    def test_lines_are_actionable_and_capped(self):
        for i in range(8):
            self.store.record("web_fetch", {"url": "https://s%d.com/x" % i}, "HTTP_ERROR", "boom")
        rows = self.store.lines(limit=3)
        self.assertEqual(len(rows), 3, "注入条数有硬上限")
        self.assertIn("web_fetch", rows[0])
        self.assertIn("换来源", rows[0])
        block = self.store.block()
        self.assertTrue(block.startswith("## 经验"))
        self.assertLessEqual(len(block.splitlines()) - 1, tl.MAX_INJECT)

    def test_trim_keeps_store_small(self):
        for i in range(tl.MAX_ENTRIES + 12):
            self.store.record("web_fetch", {"url": "https://s%d.com/x" % i}, "E", "x")
        self.assertLessEqual(len(self.store.entries()), tl.MAX_ENTRIES)

    def test_save_is_atomic_and_survives_garbage(self):
        self.store.record("web_fetch", {"url": "https://a.com/1"}, "E", "x")
        with open(self.path, encoding="utf-8") as fh:
            data = json.load(fh)
        self.assertIn("entries", data)
        leftovers = [f for f in os.listdir(self.dir) if f.startswith(".lessons-")]
        self.assertEqual(leftovers, [], "临时文件不许残留")
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("{ not json")
        broken = tl.LessonStore(self.path)
        self.assertEqual(broken.entries(), [], "坏文件当空库处理，不许抛")

    def test_error_message_trimmed(self):
        long_msg = "x" * 500
        entry = self.store.record("web_fetch", {"url": "https://a.com/1"}, "E", long_msg)
        self.assertLessEqual(len(entry["message"]), tl.KEEP_MESSAGE)


class AlternativesTest(unittest.TestCase):
    def test_known_tools_have_specific_advice(self):
        self.assertIn("web_search", tl.alternatives_for("web_fetch"))
        self.assertIn("pages", tl.alternatives_for("read_file"))
        self.assertIn("退出码", tl.alternatives_for("run_shell"))

    def test_unknown_tool_falls_back(self):
        self.assertIn("换个工具", tl.alternatives_for("no_such_tool"))


class PromptSectionTest(unittest.TestCase):
    def test_section_adds_heading_when_missing(self):
        import prompt_build
        self.assertEqual(prompt_build.lessons_section(""), "")
        self.assertEqual(prompt_build.lessons_section(None), "")
        out = prompt_build.lessons_section("- web_fetch 失败过 2 次")
        self.assertTrue(out.startswith("## 经验"))

    def test_build_includes_lessons_block(self):
        import prompt_build
        with_lessons = prompt_build.build(workspace="/tmp", lessons="## 经验\n- web_fetch 换来源")
        self.assertIn("## 经验", with_lessons)
        self.assertIn("换来源", with_lessons)
        self.assertIn("环境", with_lessons, "经验节不许挤掉环境节")

    def test_build_without_lessons_has_no_section(self):
        import prompt_build
        base = prompt_build.build(workspace="/tmp")
        self.assertNotIn("经验（过去工具失败", base)


if __name__ == "__main__":
    unittest.main()