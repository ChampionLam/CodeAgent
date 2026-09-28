"""产出文件收集（attachments in chat，2026-09-27）。"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "python"))

import artifacts  # noqa: E402


class KindTest(unittest.TestCase):
    def test_kinds(self):
        self.assertEqual(artifacts.kind_of("a.PNG"), "image")
        self.assertEqual(artifacts.kind_of("a.pdf"), "doc")
        self.assertEqual(artifacts.kind_of("a.py"), "code")
        self.assertEqual(artifacts.kind_of("a.log"), "text")
        self.assertEqual(artifacts.kind_of("a.bin"), "other")


class MarkerTest(unittest.TestCase):
    def test_marker_shapes(self):
        self.assertEqual(artifacts.marker_paths("看这个 [[file: C:/x/a.py]] 好"),
                         ["C:/x/a.py"])
        # 全角冒号 + 多余空格
        self.assertEqual(artifacts.marker_paths("[[file： C:/x/b.md ]]"), ["C:/x/b.md"])
        self.assertEqual(artifacts.marker_paths("没有标记"), [])


class WrittenTest(unittest.TestCase):
    def test_only_successful_write_tools(self):
        events = [
            {"id": "1", "name": "write_file", "ok": True, "arguments": {"path": "a.py"}},
            {"id": "2", "name": "write_file", "ok": False, "arguments": {"path": "bad.py"}},
            {"id": "3", "name": "read_file", "ok": True, "arguments": {"path": "read.py"}},
            {"id": "4", "name": "patch", "ok": True,
             "arguments": json.dumps({"path": "b.md"})},   # arguments 是字符串也认
        ]
        self.assertEqual(artifacts.written_paths(events), ["a.py", "b.md"])


class CrossNamingTest(unittest.TestCase):
    """事件流 name/arguments 与表里 tool_name/arguments_json 两种命名都要认。"""

    def test_table_style_row(self):
        rows = [{"id": "1", "tool_name": "write_file", "ok": True,
                 "arguments_json": '{"path": "x.py"}'}]
        self.assertEqual(artifacts.written_paths(rows), ["x.py"])

    def test_table_style_failure_is_skipped(self):
        rows = [{"id": "1", "tool_name": "write_file", "arguments_json": '{"path": "x.py"}',
                 "error_json": '{"code": "IO_ERROR"}'}]
        self.assertEqual(artifacts.written_paths(rows), [])


class CollectTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        for name, body in (("a.py", "print(1)"), ("img.png", "x" * 10)):
            with open(os.path.join(self.root, name), "w", encoding="utf-8") as f:
                f.write(body)

    def tearDown(self):
        self.tmp.cleanup()

    def test_collect_marker_first_then_writes_dedup_and_stat(self):
        events = [{"id": "1", "name": "write_file", "ok": True, "arguments": {"path": "a.py"}}]
        got = artifacts.collect(events, "产物见 [[file: a.py]] 与 [[file: img.png]]",
                                workspace_root=self.root)
        names = [g["name"] for g in got]
        self.assertEqual(names, ["a.py", "img.png"])          # 标记优先，写盘去重后不再重复
        self.assertEqual(got[0]["kind"], "code")
        self.assertEqual(got[1]["kind"], "image")
        self.assertGreater(got[1]["size"], 0)

    def test_missing_files_are_skipped(self):
        got = artifacts.collect([], "[[file: nope.py]]", workspace_root=self.root)
        self.assertEqual(got, [])

    def test_cap(self):
        events = []
        for i in range(20):
            with open(os.path.join(self.root, "f%d.py" % i), "w", encoding="utf-8") as f:
                f.write("x")
            events.append({"id": str(i), "name": "write_file", "ok": True,
                           "arguments": {"path": "f%d.py" % i}})
        self.assertEqual(len(artifacts.collect(events, "", workspace_root=self.root)),
                         artifacts.MAX_ARTIFACTS)


if __name__ == "__main__":
    unittest.main()