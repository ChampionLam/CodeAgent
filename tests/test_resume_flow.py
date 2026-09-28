"""sidecar 断点接线单测：续跑重建、断点回调语义、run.* RPC。

全部走临时数据目录，不碰应用真实数据。
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "python"))

import run_checkpoint as rc  # noqa: E402
import sidecar  # noqa: E402


class FakeSessionStore:
    def __init__(self, rows):
        self.rows = rows
        self.asked = []

    def get_messages(self, session_id, include_inactive=False):
        self.asked.append(session_id)
        return [dict(r) for r in self.rows]


def _res(out, key):
    """从 RPC 信封里取结果（不同版本可能在 result 里或平铺）。"""
    body = out.get("result") if isinstance(out.get("result"), dict) else out
    return body.get(key)


class ResumeFlowTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="resume-sidecar-")
        self.store = rc.RunStore(os.path.join(self.dir, "runs"))
        self._runs_backup = sidecar._RUNS
        self._store_backup = sidecar._session_store
        sidecar._RUNS = self.store

    def tearDown(self):
        sidecar._RUNS = self._runs_backup
        sidecar._session_store = self._store_backup
        shutil.rmtree(self.dir, ignore_errors=True)

    # ---------- 续跑 ----------
    def test_partial_event_is_stored_and_cleared_on_new_round(self):
        rec = self.store.start(session_id="s-1")
        handler = sidecar._checkpoint_handler(rec["run_id"])
        handler({"event": "partial", "text": "我先看了前八页", "rounds": 2})
        self.assertEqual(self.store.get(rec["run_id"])["partial_text"], "我先看了前八页")

        handler({"event": "round", "rounds": 3, "max_loop": 40, "done_calls": []})
        got = self.store.get(rec["run_id"])
        self.assertEqual(got["partial_text"], "", "新的一轮开始，上一轮正文已落库，快照要清掉")
        self.assertEqual(got["rounds"], 3)

    def test_partial_cleared_when_run_finishes(self):
        rec = self.store.start(session_id="s-1")
        handler = sidecar._checkpoint_handler(rec["run_id"])
        handler({"event": "partial", "text": "写了一半"})
        self.assertEqual(self.store.get(rec["run_id"])["partial_text"], "写了一半")
        handler({"event": "end", "status": "done"})
        got = self.store.get(rec["run_id"])
        self.assertEqual(got["status"], "done")
        self.assertEqual(got["partial_text"], "", "跑完了就不该再留半截快照")

    def test_partial_kept_when_interrupted(self):
        rec = self.store.start(session_id="s-1")
        handler = sidecar._checkpoint_handler(rec["run_id"])
        handler({"event": "partial", "text": "写了一半就断网"})
        handler({"event": "end", "status": "interrupted", "reason": "网络中断"})
        got = self.store.get(rec["run_id"])
        self.assertEqual(got["status"], "interrupted")
        self.assertEqual(got["partial_text"], "写了一半就断网", "中断必须留着半截正文")

    def test_resume_note_carries_partial_text(self):
        rec = self.store.start(session_id="s-1")
        self.store.update(rec["run_id"], rounds=2, done_calls=["c1"])
        self.store.note_partial(rec["run_id"], "已经写出半句结论：建议先去长寿村", round_no=2)
        self.store.interrupt(rec["run_id"], reason="应用关闭")
        sidecar._session_store = lambda: FakeSessionStore(
            [{"role": "user", "content": "整理通天河攻略"}])

        msgs, resumed = sidecar._apply_resume(
            {"resumeRunId": rec["run_id"], "sessionId": "s-1"}, [])
        self.assertEqual(resumed, rec["run_id"])
        note = msgs[-1]["content"]
        self.assertEqual(msgs[-1]["role"], "system")
        self.assertIn("[续跑]", note)
        self.assertIn("建议先去长寿村", note, "半截正文要喂回给模型接话头")
        self.assertIn("不要从头再写一遍", note)

    def test_resume_keeps_partial_until_the_turn_really_finishes(self):
        """2026-09-27 改过口径：重建上下文时先不动断点，等这轮真跑完才收尾。

        理由（_apply_resume 里的注释）：请求还没发出去就把断点标成「已续跑」，
        厂商一报错（HTTP 400 之类）断点已被消费掉、ResumeBar 也消失，
        用户连重试的机会都没有。收尾挪到了 _close_resumed_run。
        """
        rec = self.store.start(session_id="s-1")
        self.store.note_partial(rec["run_id"], "写到一半的结论")
        self.store.interrupt(rec["run_id"], reason="应用关闭")
        sidecar._session_store = lambda: FakeSessionStore(
            [{"role": "user", "content": "整理攻略"}])

        msgs, _ = sidecar._apply_resume({"resumeRunId": rec["run_id"], "sessionId": "s-1"}, [])
        self.assertIn("写到一半的结论", msgs[-1]["content"], "喂之前得还在")
        still = self.store.get(rec["run_id"])
        self.assertEqual(still["partial_text"], "写到一半的结论", "这一轮还没跑完，不许清")
        self.assertEqual(still["status"], rc.STATUS_INTERRUPTED, "还没跑完就得还能重试")

        # 这一轮真跑完了，才收尾：清掉半截正文 + 标 done
        sidecar._close_resumed_run(rec["run_id"])
        done = self.store.get(rec["run_id"])
        self.assertEqual(done["partial_text"], "", "喂过了就该清掉")
        self.assertEqual(done["status"], rc.STATUS_DONE)

    def test_resume_note_without_partial_stays_short(self):
        rec = self.store.start(session_id="s-1")
        self.store.interrupt(rec["run_id"], reason="网络中断")
        note = sidecar._resume_note(self.store.get(rec["run_id"]))
        self.assertIn("[续跑]", note)
        self.assertNotIn("[中断时你正在写的正文]", note)

    def test_resume_rebuilds_from_session_db_and_notes_it(self):
        rec = self.store.start(session_id="s-1")
        self.store.update(rec["run_id"], rounds=3, done_calls=["c1", "c2"])
        self.store.interrupt(rec["run_id"], reason="应用关闭")
        sidecar._session_store = lambda: FakeSessionStore(
            [{"role": "user", "content": "帮我整理攻略"},
             {"role": "assistant", "content": "前 8 页看完了"}])

        msgs, resumed = sidecar._apply_resume({"resumeRunId": rec["run_id"], "sessionId": "s-1"},
                                              [{"role": "user", "content": "客户端那份"}])
        self.assertEqual(resumed, rec["run_id"])
        self.assertEqual(len(msgs), 3, "两条历史 + 一条续跑交代")
        self.assertEqual(msgs[0]["content"], "帮我整理攻略", "用会话库重放，不信客户端的")
        self.assertEqual(msgs[-1]["role"], "system", "交代用 system，不会被当成用户发言落库")
        self.assertIn("续跑", msgs[-1]["content"])
        self.assertIn("应用关闭", msgs[-1]["content"])
        # 这一轮还没真跑完，断点先留着（报错可重试）；跑完由 _close_resumed_run 收尾。
        self.assertEqual(self.store.get(rec["run_id"])["status"], rc.STATUS_INTERRUPTED,
                         "重建上下文时先别消费断点")
        sidecar._close_resumed_run(rec["run_id"])
        self.assertEqual(self.store.get(rec["run_id"])["status"], rc.STATUS_DONE,
                         "续跑这轮跑完就不该再被提示「继续」")

    def test_resume_ignores_unknown_or_finished_run(self):
        rec = self.store.start(session_id="s-1")
        self.store.finish(rec["run_id"])
        msgs, resumed = sidecar._apply_resume({"resumeRunId": rec["run_id"]}, [{"role": "user", "content": "x"}])
        self.assertEqual(resumed, "")
        self.assertEqual(msgs, [{"role": "user", "content": "x"}])

        msgs2, resumed2 = sidecar._apply_resume({"resumeRunId": "r-nope"}, [{"role": "user", "content": "x"}])
        self.assertEqual(resumed2, "")
        self.assertEqual(len(msgs2), 1)

    def test_resume_without_flag_is_noop(self):
        msgs, resumed = sidecar._apply_resume({}, [{"role": "user", "content": "hi"}])
        self.assertEqual(resumed, "")
        self.assertEqual(len(msgs), 1)

    def test_resume_survives_broken_session_store(self):
        rec = self.store.start(session_id="s-1")
        self.store.interrupt(rec["run_id"])
        sidecar._session_store = lambda: (_ for _ in ()).throw(RuntimeError("db gone"))
        msgs, resumed = sidecar._apply_resume({"resumeRunId": rec["run_id"]},
                                              [{"role": "user", "content": "client"}])
        self.assertEqual(resumed, rec["run_id"], "会话库坏了也要能续，退回客户端历史")
        self.assertEqual(msgs[0]["content"], "client")
        self.assertEqual(msgs[-1]["role"], "system")

    # ---------- 断点回调 ----------
    def test_checkpoint_handler_round_and_end(self):
        rec = self.store.start(session_id="s-1")
        handler = sidecar._checkpoint_handler(rec["run_id"])
        handler({"event": "round", "rounds": 2, "max_loop": 25, "done_calls": ["c1"]})
        got = self.store.get(rec["run_id"])
        self.assertEqual(got["rounds"], 2)
        self.assertEqual(got["max_loop"], 25)
        self.assertEqual(got["done_calls"], ["c1"])
        handler({"event": "end", "status": "done"})
        self.assertEqual(self.store.get(rec["run_id"])["status"], rc.STATUS_DONE)

    def test_checkpoint_end_interrupted_is_resumable(self):
        rec = self.store.start(session_id="s-1")
        handler = sidecar._checkpoint_handler(rec["run_id"])
        handler({"event": "end", "status": "interrupted", "reason": "网络中断",
                 "interrupt_hint": "NETWORK"})
        got = self.store.get(rec["run_id"])
        self.assertEqual(got["status"], rc.STATUS_INTERRUPTED)
        self.assertEqual(got["reason"], "网络中断")
        self.assertIsNotNone(self.store.pending_for_session("s-1"))

    def test_interrupt_is_not_overwritten_by_end(self):
        """先中断、后收尾：中断必须保住（否则用户看不到「继续」）。"""
        rec = self.store.start(session_id="s-1")
        handler = sidecar._checkpoint_handler(rec["run_id"])
        handler({"event": "interrupt", "reason": "网络中断"})
        handler({"event": "end", "status": "failed", "reason": "NETWORK"})
        self.assertEqual(self.store.get(rec["run_id"])["status"], rc.STATUS_INTERRUPTED)

    def test_checkpoint_handler_never_raises(self):
        handler = sidecar._checkpoint_handler("r-不存在的运行")
        handler({"event": "round", "rounds": 1})
        handler({"event": "end", "status": "done"})

    # ---------- RPC ----------
    def test_run_rpcs(self):
        rec = self.store.start(session_id="s-1")
        out = sidecar.handle_request({"id": "1", "method": "run.active", "params": {"protocolVersion": 1}})
        runs = out["result"]["runs"] if "result" in out else out["runs"]
        self.assertEqual([r["run_id"] for r in runs], [rec["run_id"]])

        out = sidecar.handle_request({"id": "2", "method": "run.pending",
                                      "params": {"protocolVersion": 1, "sessionId": "s-1"}})
        self.assertIsNone(_res(out, "run"), "还在跑的不算可续")

        out = sidecar.handle_request({"id": "3", "method": "run.interrupt",
                                      "params": {"protocolVersion": 1, "runId": rec["run_id"], "reason": "关程序"}})
        self.assertEqual(_res(out, "run")["status"], rc.STATUS_INTERRUPTED)

        out = sidecar.handle_request({"id": "4", "method": "run.pending",
                                      "params": {"protocolVersion": 1, "sessionId": "s-1"}})
        self.assertEqual(_res(out, "run")["run_id"], rec["run_id"])
        self.assertEqual(_res(out, "run")["reason"], "关程序")

        out = sidecar.handle_request({"id": "5", "method": "run.active",
                                      "params": {"protocolVersion": 1}})
        self.assertIsNotNone(_res(out, "runs"))
        self.assertIn("run.pending", sidecar.SIDECAR_CAPABILITIES)


if __name__ == "__main__":
    unittest.main()