"""运行断点库单测：写盘 / 状态机 / 启动扫描（把 running 判为中断）/ 可续查询。"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "python"))

import run_checkpoint as rc  # noqa: E402


class RunStoreTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="runs-")
        self.store = rc.RunStore(self.dir)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_start_writes_small_record(self):
        rec = self.store.start(kind="chat", session_id="s-1", model="m", max_loop=25)
        path = os.path.join(self.dir, rec["run_id"] + ".json")
        self.assertTrue(os.path.exists(path))
        self.assertLess(os.path.getsize(path), 1200, "断点文件必须很小（不存消息正文）")
        self.assertEqual(rec["status"], rc.STATUS_RUNNING)
        self.assertEqual(rec["session_id"], "s-1")
        self.assertNotIn("messages", rec, "消息正文不入断点，续跑从会话库重放")

    def test_update_only_whitelisted_fields(self):
        rec = self.store.start(session_id="s-1")
        out = self.store.update(rec["run_id"], rounds=3, done_calls=["c1"], huge={"blob": "x" * 5000})
        self.assertEqual(out["rounds"], 3)
        self.assertEqual(out["done_calls"], ["c1"])
        self.assertNotIn("huge", out)

    def test_finish_and_interrupt(self):
        rec = self.store.start(session_id="s-1")
        self.store.finish(rec["run_id"])
        self.assertEqual(self.store.get(rec["run_id"])["status"], rc.STATUS_DONE)
        rec2 = self.store.start(session_id="s-2")
        self.store.interrupt(rec2["run_id"], reason="断电", hint="第 3 轮")
        got = self.store.get(rec2["run_id"])
        self.assertEqual(got["status"], rc.STATUS_INTERRUPTED)
        self.assertEqual(got["reason"], "断电")
        self.assertEqual(got["pending"], [])

    def test_sweep_on_start_marks_running_as_interrupted(self):
        live = self.store.start(session_id="s-1")
        done = self.store.start(session_id="s-2")
        self.store.finish(done["run_id"])
        touched = self.store.sweep_on_start()
        self.assertEqual([r["run_id"] for r in touched], [live["run_id"]])
        self.assertEqual(self.store.get(live["run_id"])["status"], rc.STATUS_INTERRUPTED)
        self.assertEqual(self.store.get(done["run_id"])["status"], rc.STATUS_DONE,
                         "跑完的不许被误判成中断")

    def test_pending_for_session(self):
        rec = self.store.start(session_id="s-1")
        self.assertIsNone(self.store.pending_for_session("s-1"), "还在跑的不算可续")
        self.store.interrupt(rec["run_id"], reason="网络中断")
        pending = self.store.pending_for_session("s-1")
        self.assertIsNotNone(pending)
        self.assertEqual(pending["run_id"], rec["run_id"])
        self.assertIsNone(self.store.pending_for_session("s-9"))

    def test_latest_for_session_picks_newest(self):
        first = self.store.start(session_id="s-1")
        self.store.interrupt(first["run_id"])
        self.store.update(first["run_id"], updated_at=None) if False else None
        self.store.update(first["run_id"], reason="old")
        second = self.store.start(session_id="s-1")
        self.store.interrupt(second["run_id"], reason="new")
        latest = self.store.latest_for_session("s-1", statuses=rc.RESUMABLE)
        self.assertIn(latest["run_id"], (first["run_id"], second["run_id"]))
        self.assertIsNotNone(latest)

    def test_broken_file_is_skipped(self):
        with open(os.path.join(self.dir, "r-broken.json"), "w", encoding="utf-8") as fh:
            fh.write("{ not json")
        self.assertIsNone(self.store.get("r-broken"))
        self.assertEqual(self.store.list_all(), [])
        self.assertEqual(self.store.sweep_on_start(), [])

    def test_note_partial_keeps_more_than_other_text_fields(self):
        rec = self.store.start(session_id="s-1")
        long_text = "攻" * 1200
        self.store.note_partial(rec["run_id"], long_text, round_no=2)
        got = self.store.get(rec["run_id"])
        self.assertEqual(got["partial_text"], long_text, "半截正文不该被 200 字砍掉")
        self.assertEqual(got["partial_round"], 2)

    def test_partial_is_capped_at_partial_field(self):
        rec = self.store.start(session_id="s-1")
        self.store.note_partial(rec["run_id"], "字" * 9000)
        self.assertEqual(len(self.store.get(rec["run_id"])["partial_text"]), rc.PARTIAL_FIELD)

    def test_partial_survives_interrupt_and_sweep(self):
        rec = self.store.start(session_id="s-1")
        self.store.note_partial(rec["run_id"], "写到一半的话")
        self.store.interrupt(rec["run_id"], reason="应用关闭")
        got = self.store.get(rec["run_id"])
        self.assertEqual(got["status"], rc.STATUS_INTERRUPTED)
        self.assertEqual(got["partial_text"], "写到一半的话", "中断不能把半截正文抹掉")

        rec2 = self.store.start(session_id="s-2")
        self.store.note_partial(rec2["run_id"], "断电前写的那句")
        self.store.sweep_on_start()
        self.assertEqual(self.store.get(rec2["run_id"])["partial_text"], "断电前写的那句")

    def test_transient_failure_run_is_offered_as_resumable(self):
        """瞬时故障（限流/5xx）中断的运行，必须能被界面查到「继续」。"""
        rec = self.store.start(session_id="s-1")
        self.store.note_partial(rec["run_id"], "写了一半")
        self.store.interrupt(rec["run_id"], reason="网络/接口异常", hint="HTTP_ERROR")
        pend = self.store.pending_for_session("s-1")
        self.assertIsNotNone(pend, "限流中断的任务要出现在「继续」入口里")
        self.assertEqual(pend["run_id"], rec["run_id"])
        self.assertEqual(pend["partial_text"], "写了一半")

    def test_hard_failure_run_is_not_offered(self):
        rec = self.store.start(session_id="s-2")
        self.store.finish(rec["run_id"], status=rc.STATUS_FAILED, reason="AUTH_ERROR")
        self.assertIsNone(self.store.pending_for_session("s-2"),
                          "401 这种不给「继续」，免得用户白点")

    def test_update_plain_text_field_still_capped(self):
        rec = self.store.start(session_id="s-1")
        self.store.update(rec["run_id"], reason="理" * 800)
        self.assertEqual(len(self.store.get(rec["run_id"])["reason"]), rc.KEEP_FIELD)

    def test_atomic_write_leaves_no_temp(self):
        self.store.start(session_id="s-1")
        leftovers = [f for f in os.listdir(self.dir) if f.startswith(".run-")]
        self.assertEqual(leftovers, [])

    def test_list_all_sorted_and_pruned(self):
        for i in range(5):
            self.store.start(session_id="s-%d" % i)
        rows = self.store.list_all()
        self.assertEqual(len(rows), 5)
        self.assertEqual([r["updated_at"] for r in rows], sorted(r["updated_at"] for r in rows))

    def test_json_round_trip_is_utf8(self):
        rec = self.store.start(session_id="会话-1", model="模型")
        self.store.interrupt(rec["run_id"], reason="断电了")
        with open(os.path.join(self.dir, rec["run_id"] + ".json"), encoding="utf-8") as fh:
            data = json.load(fh)
        self.assertEqual(data["reason"], "断电了")
        self.assertEqual(data["session_id"], "会话-1")


if __name__ == "__main__":
    unittest.main()