"""「续跑横幅该不该亮」的口径（2026-10-14 真机缺陷）。

真机症状：同一条断点被续两次（日志 03:18:07 与 03:18:16 都是 resume: r-…4194），
而且跑完的轮次不收尾 —— 重新打开应用又冒一条「上次任务未完成」。界面会在
chat.done / chat.error / 切会话 / 启动时都拉一次 run.pending，所以判定必须严：
正在跑（或有后人接手）的会话，不许再把旧断点亮出来。
"""
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "python"))

import run_checkpoint as rc  # noqa: E402


class PendingBannerTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="run-pending-")
        self.store = rc.RunStore(self.dir)

    def test_interrupted_run_is_pending(self):
        rec = self.store.start(kind="chat", session_id="s1")
        self.store.interrupt(rec["run_id"], reason="断网")
        got = self.store.pending_for_session("s1")
        self.assertIsNotNone(got, "被中断的断点该亮「继续」")
        self.assertEqual(got["run_id"], rec["run_id"])

    def test_running_successor_hides_the_banner(self):
        old = self.store.start(kind="chat", session_id="s1")
        self.store.interrupt(old["run_id"], reason="断网")
        self.store.start(kind="chat", session_id="s1", resumed_from=old["run_id"])
        self.assertIsNone(self.store.pending_for_session("s1"),
                          "续跑正在跑的时候不该再亮旧断点（亮了就会被点第二次，双烧）")

    def test_claimed_checkpoint_is_not_pending_again(self):
        old = self.store.start(kind="chat", session_id="s1")
        self.store.interrupt(old["run_id"], reason="断网")
        new = self.store.start(kind="chat", session_id="s1", resumed_from=old["run_id"])
        self.store.finish(new["run_id"], status=rc.STATUS_DONE)
        self.assertIsNone(self.store.pending_for_session("s1"),
                          "已经续过并跑完的断点不该再亮（真机上就是这么反复弹的）")

    def test_failed_successor_becomes_the_pending_one(self):
        old = self.store.start(kind="chat", session_id="s1")
        self.store.interrupt(old["run_id"], reason="断网")
        new = self.store.start(kind="chat", session_id="s1", resumed_from=old["run_id"])
        self.store.interrupt(new["run_id"], reason="又断网")
        got = self.store.pending_for_session("s1")
        self.assertIsNotNone(got)
        self.assertEqual(got["run_id"], new["run_id"],
                         "要续的是最新那条，不是最早那条")

    def test_other_session_is_not_affected(self):
        a = self.store.start(kind="chat", session_id="s1")
        self.store.interrupt(a["run_id"], reason="断网")
        b = self.store.start(kind="chat", session_id="s2")
        self.store.interrupt(b["run_id"], reason="断网")
        self.store.start(kind="chat", session_id="s1", resumed_from=a["run_id"])
        got = self.store.pending_for_session("s2")
        self.assertIsNotNone(got, "别的会话的断点不受影响")
        self.assertEqual(got["run_id"], b["run_id"])


if __name__ == "__main__":
    unittest.main()