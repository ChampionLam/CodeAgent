"""
reasoning_split 的单元测试。

跑法（两种都支持）：
    cd <repo> && python3 -m unittest tests.test_reasoning_split -v
    python3 tests/test_reasoning_split.py
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "python"))

from reasoning_split import ReasoningSplitter, split_stream, HOLD_MAX, MAX_TOKEN_LEN  # noqa: E402

LT, GT = "\x3c", "\x3e"


def tag(name: str, close: bool = False) -> str:
    """拼出一个标签字面。用 \x3c/\x3e 转义，避免源码里的尖括号被外部工具吃掉。"""
    return LT + ("/" if close else "") + name + GT


class TestConstants(unittest.TestCase):
    def test_hold_max_derived_not_hardcoded(self):
        # 最长 token 是 </reasoning_scratchpad>（2 + 20 + 1 = 23）
        self.assertEqual(MAX_TOKEN_LEN, 23)
        self.assertEqual(HOLD_MAX, 22)

    def test_longest_prefix_of_longest_token_is_held(self):
        """22 字符边界：最长 token 去掉最后一个字符，必须整段挂起不被当正文发出。"""
        long_prefix = tag("reasoning_scratchpad", close=True)[:-1]   # 22 字符
        self.assertEqual(len(long_prefix), 22)
        sp = ReasoningSplitter()
        self.assertEqual(sp.feed(long_prefix), ("", ""))            # 必须挂起
        self.assertEqual(sp.feed(GT), ("", ""))                     # 补上 > 完成 token
        self.assertEqual(sp.feed("x"), ("", "x"))                   # 之后正常出正文


class TestSplitter(unittest.TestCase):
    def test_01_plain_content_no_tags(self):
        sp = ReasoningSplitter()
        self.assertEqual(sp.feed("hello"), ("", "hello"))
        self.assertEqual(sp.flush(), ("", ""))

    def test_02_full_pair_inside_content(self):
        sp = ReasoningSplitter()
        r, c = sp.feed("a " + tag("think") + "cil" + tag("think", True) + "\n\nans")
        self.assertEqual((r, c), ("cil", "a ans"))

    def test_03_open_tag_split_across_deltas(self):
        sp = ReasoningSplitter()
        self.assertEqual(sp.feed(LT + "thi"), ("", ""))        # 挂起
        self.assertEqual(sp.feed("nk" + GT + "b"), ("b", ""))  # 标签拼完，进 reasoning
        self.assertEqual(sp.feed(LT + "/thi"), ("", ""))       # 闭标签再挂起
        self.assertEqual(sp.feed("nk" + GT + "c"), ("", "c"))
        self.assertEqual(sp.flush(), ("", ""))

    def test_04_close_tag_split_across_deltas(self):
        sp = ReasoningSplitter()
        sp.feed(tag("think") + "body")
        self.assertEqual(sp.feed(LT + "/think"), ("", ""))     # 闭标签挂起
        self.assertEqual(sp.feed(GT + "tail"), ("", "tail"))

    def test_05_unterminated_open_tag_truncated(self):
        """MiniMax 截断时会丢闭标签：剩下的文本必须作为 reasoning 发出，不能丢。"""
        sp = ReasoningSplitter()
        self.assertEqual(sp.feed("a"), ("", "a"))
        self.assertEqual(sp.feed(tag("think") + "orphan"), ("orphan", ""))
        self.assertEqual(sp.flush(), ("", ""))

    def test_06_empty_string(self):
        sp = ReasoningSplitter()
        self.assertEqual(sp.feed(""), ("", ""))
        self.assertEqual(sp.flush(), ("", ""))

    def test_07_case_insensitive(self):
        sp = ReasoningSplitter()
        r, c = sp.feed("A" + LT + "THINK" + GT + "B" + LT + "/Think" + GT + "C")
        self.assertEqual((r, c), ("B", "AC"))

    def test_08_multiple_blocks(self):
        sp = ReasoningSplitter()
        r, c = sp.feed("a" + tag("think") + "b" + tag("think", True)
                       + "c" + tag("think") + "d" + tag("think", True) + "e")
        self.assertEqual((r, c), ("bd", "ace"))

    def test_09_whitespace_after_close_tag_stripped(self):
        sp = ReasoningSplitter()
        r, c = sp.feed(tag("think") + "x" + tag("think", True) + "\n\n 晴")
        self.assertEqual((r, c), ("x", "晴"))

    def test_10_orphan_close_tag_in_content_dropped(self):
        sp = ReasoningSplitter()
        r, c = sp.feed("a" + tag("think", True) + "b")
        self.assertEqual((r, c), ("", "ab"))

    def test_11_whitespace_arrives_in_later_delta(self):
        """skip_ws 必须跨 delta 生效：闭标签和它的换行落在不同 delta。"""
        sp = ReasoningSplitter()
        sp.feed(tag("think") + "x" + tag("think", True))
        self.assertEqual(sp.feed("\n\n"), ("", ""))
        self.assertEqual(sp.feed("晴"), ("", "晴"))

    def test_12_all_five_tags_recognized(self):
        for name in ("REASONING_SCRATCHPAD", "think", "thinking", "reasoning", "thought"):
            with self.subTest(tag=name):
                sp = ReasoningSplitter()
                r, c = sp.feed("p" + tag(name) + "q" + tag(name, True) + "r")
                self.assertEqual((r, c), ("q", "pr"))

    def test_13_nothing_is_lost(self):
        """两条通道拼起来必须覆盖所有非标签字符（不丢字）。"""
        chunks = ["a", LT, "thi", "nk", GT, " reasoned ", LT, "/th", "ink", GT, "\n\n", "done"]
        r, c = split_stream(chunks)
        self.assertEqual(r, " reasoned ")
        self.assertEqual(c, "adone")


if __name__ == "__main__":
    unittest.main(verbosity=2)
