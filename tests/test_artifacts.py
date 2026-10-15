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

class GeneratedMediaTest(unittest.TestCase):
    """生图/生视频的产物必须进附件列表（2026-10-14 真机：文件真生成了，聊天里却看不见）。"""

    def test_image_result_files_are_collected(self):
        rows = [{"id": "1", "name": "image_generate", "ok": True,
                 "arguments": {"prompt": "cat"},
                 "result": {"ok": True,
                            "content": "image_generate: 已生成 1 个文件\nD:/ws/generated/gen_1.png",
                            "data": {"files": ["D:/ws/generated/gen_1.png"]}}}]
        self.assertEqual(artifacts.generated_paths(rows), ["D:/ws/generated/gen_1.png"])

    def test_failed_gen_is_skipped(self):
        rows = [{"id": "1", "name": "video_generate", "ok": False,
                 "result": {"ok": False, "content": "video_generate: 失败 AUTH"}}]
        self.assertEqual(artifacts.generated_paths(rows), [])
        rows2 = [{"id": "2", "name": "image_generate", "ok": True,
                  "result": {"ok": False, "content": "image_generate: AUTH"}}]
        self.assertEqual(artifacts.generated_paths(rows2), [])

    def test_table_naming_and_content_fallback_recognize_media_ext(self):
        rows = [{"id": "1", "tool_name": "video_generate", "ok": True,
                 "result_json": json.dumps({"ok": True, "content":
                    "video_generate: 已生成 1 个文件\nD:\\ws\\gen\\gen_9.mp4"})}]
        self.assertEqual(artifacts.generated_paths(rows), ["D:\\ws\\gen\\gen_9.mp4"])

    def test_collect_includes_generated_media(self):
        d = tempfile.mkdtemp()
        png = os.path.join(d, "gen_x.png")
        with open(png, "wb") as fh:
            fh.write(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
        rows = [{"id": "1", "name": "image_generate", "ok": True,
                 "result": {"ok": True, "content": "image_generate: 已生成 1 个文件",
                            "data": {"files": [png]}}}]
        got = artifacts.collect(rows, "图好了", workspace_root=d)
        self.assertEqual([a["path"] for a in got], [png])
        self.assertEqual(got[0]["kind"], "image")

    def test_live_captured_row_shape_is_supported(self):
        """事件流捕获的行是扁平的：只有 name/ok/content，没有 result 包装。

        2026-10-14 真机：后端改了但聊天里还是不显示 —— 因为线上走的是这条
        chat_persist.merge_tool_event 造出来的扁平行，而不是表里的 result_json。
        """
        rows = [{"id": "1", "name": "image_generate", "ok": True,
                 "content": "image_generate: 已生成 1 个文件\nD:/ws/gen/gen_7.png"}]
        self.assertEqual(artifacts.generated_paths(rows), ["D:/ws/gen/gen_7.png"])

    def test_live_captured_video_row_is_supported(self):
        rows = [{"id": "2", "name": "video_generate", "ok": True,
                 "content": "video_generate: 已生成 1 个文件\nD:/ws/gen/gen_9.mp4\nmodel: MiniMax-Hailuo"}]
        self.assertEqual(artifacts.generated_paths(rows), ["D:/ws/gen/gen_9.mp4"])

    def test_video_and_audio_have_their_own_kind(self):
        """视频/音频单独成类 —— 界面据此出内嵌播放器（2026-10-15）。"""
        self.assertEqual(artifacts.kind_of("gen_1.mp4"), "video")
        self.assertEqual(artifacts.kind_of("a.WEBM"), "video")
        self.assertEqual(artifacts.kind_of("a.mov"), "video")
        self.assertEqual(artifacts.kind_of("voice.mp3"), "audio")
        self.assertEqual(artifacts.kind_of("a.wav"), "audio")
        self.assertEqual(artifacts.kind_of("shot.png"), "image")
