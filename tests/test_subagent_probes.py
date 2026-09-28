"""真探测的回归用例（2026-09-26）。

起因：`available_memory_bytes()` 在 Windows 上因为 ctypes 结构体少两个字段，
`GlobalMemoryStatusEx` 调用失败返回 0，代码却把 0 当事实用 → 降级到 1 个节点。
原来那批性能用例全是**注入数值**的，所以全绿也发现不了 —— 这条用例专门打真探测：
读得到就必须是正数，读不到必须是 None，不许拿 0 当事实。
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "python"))

import subagent  # noqa: E402

_GB = 1024 * 1024 * 1024


class TestRealProbes(unittest.TestCase):
    def test_available_memory_is_positive_or_none(self):
        """真探测：要么给出正数，要么明确说读不到；0 是 bug 不是事实。"""
        val = subagent.available_memory_bytes()
        if val is None:
            self.skipTest("platform did not expose available memory; None is acceptable")
        self.assertGreater(val, 0, "available_memory_bytes must never return 0 as a fact")
        self.assertNotEqual(val, 0)

    @unittest.skipUnless(sys.platform.startswith("win"), "Windows-only probe")
    def test_windows_available_memory_is_plausible(self):
        """Windows 上必须真的读到内存（这台机器不可能是 0，也不可能小于 1GB）。"""
        val = subagent.available_memory_bytes()
        self.assertIsNotNone(val, "GlobalMemoryStatusEx must succeed on Windows")
        self.assertGreater(val, 1 * _GB)

    def test_rss_probe_is_positive(self):
        """当前进程 RSS 也要是正数（同一个 ctypes 坑会在别处复现）。"""
        val = subagent.rss_bytes()
        if val is None:
            self.skipTest("platform did not expose rss")
        self.assertGreater(val, 0)

    @unittest.skipUnless(sys.platform.startswith("win"), "Windows-only probe")
    def test_effective_limit_is_not_starved_by_a_broken_probe(self):
        """Windows 上 4 核 8GB 级别的机器不该只给 1 个节点（探测读到 0 才会这样）。"""
        avail = subagent.available_memory_bytes()
        limit, reason = subagent.effective_limit()
        self.assertGreaterEqual(limit, 1)
        if avail is not None and avail >= 8 * _GB:
            self.assertGreaterEqual(
                limit, 2,
                "8GB+ available memory must not degrade to a single node (reason: %s)"
                % reason)


if __name__ == "__main__":
    unittest.main()