"""vision 预读注入前「只留正文」的单元测试（sidecar._vision_text_only）。

背景（2026-09-28）：会话模型没声明吃图时，图片先交给 vision 线路读成文字再进
主对话上下文。MiniMax 这类端点把思考内联在正文里（``<thinking>…</thinking>``），
以前直接用 chat_messages_once 的原文注入，把模型整段思考也塞进了上下文。

跑法（两种都支持）：
    cd <repo> && python3 -m unittest tests.test_vision_text_only -v
    python3 tests/test_vision_text_only.py

硬约束：不打网络、不写项目目录、不碰用户 config（本文件全是纯字符串函数）。
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "python"))

import sidecar  # noqa: E402


class TestVisionTextOnly(unittest.TestCase):

    def test_thinking_tag_stripped(self):
        """带 <thinking> 标签 → 只留正文，思考不进上下文。"""
        got = sidecar._vision_text_only("<thinking>用户要求描述图片，我先看图……</thinking>图里写着「你好」。")
        self.assertEqual(got, "图里写着「你好」。")

    def test_bare_thinking_tag_stripped(self):
        """<think> 也是标签集里的一个。"""
        got = sidecar._vision_text_only("<think>先想一下</think>答案：4713")
        self.assertEqual(got, "答案：4713")

    def test_plain_text_untouched(self):
        """模型没用标签 → 原文照旧，一个字都不改。"""
        src = "这是一张白底黑字的截图，标题是 CodeAgent。"
        self.assertEqual(sidecar._vision_text_only(src), src)

    def test_body_before_and_after_tag_kept(self):
        """标签夹在中间：两侧正文都保留，思考丢弃。"""
        got = sidecar._vision_text_only("前一句。<thinking>中间是草稿</thinking>后一句。")
        self.assertEqual(got, "前一句。后一句。")

    def test_all_thinking_falls_back_to_raw(self):
        """整段都是思考（分流后正文为空）→ 退回原文，绝不把内容吃空。"""
        src = "<thinking>只有思考没有正文</thinking>"
        self.assertEqual(sidecar._vision_text_only(src), src)

    def test_empty_and_none(self):
        self.assertEqual(sidecar._vision_text_only(""), "")
        self.assertEqual(sidecar._vision_text_only(None), "")

    def test_never_raises_on_weird_input(self):
        """非字符串输入不许抛（注入失败不能把整轮对话带崩）。"""
        for bad in (0, [], {}, object()):
            out = sidecar._vision_text_only(bad)
            self.assertIsInstance(out, str)


if __name__ == "__main__":
    unittest.main(verbosity=2)