"""Compaction persistence: audit rows, the spill index, and the governor hooks.

The governor stays storage-agnostic, so the two halves are tested apart: the
store writers here, the hooks that feed them just below. The wiring that joins
them lives in the sidecar's chat handler and is exercised on the real machine.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "python"))  # noqa: E402

import context_mechanism  # noqa: E402
import sessionstore  # noqa: E402


class StoreWriterTest(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.mkdtemp(prefix="desk-compaction-")
        self.store = sessionstore.SessionStore(os.path.join(self.dir, "sessions.db"))
        self.sid = self.store.create_session(model="MiniMax-M3", base_url=None, title="t")

    def tearDown(self) -> None:
        self.store.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def _records(self):
        return self.store._conn.execute(
            "SELECT generation, trigger, tokens_before, tokens_after, summary_text"
            " FROM compaction_records WHERE session_id = ? ORDER BY generation",
            (self.sid,)).fetchall()

    def test_replaying_a_generation_updates_instead_of_duplicating(self) -> None:
        self.store.append_compaction_record(
            session_id=self.sid, generation=1, trigger="pressure",
            tokens_before=9000, tokens_after=3000, summary_text="第一版摘要")
        self.store.append_compaction_record(
            session_id=self.sid, generation=1, trigger="pressure",
            tokens_before=9000, tokens_after=2500, summary_text="第二版摘要")

        rows = self._records()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][3], 2500)
        self.assertEqual(rows[0][4], "第二版摘要")

    def test_each_generation_gets_its_own_row(self) -> None:
        for gen in (1, 2):
            self.store.append_compaction_record(
                session_id=self.sid, generation=gen, trigger="context-overflow",
                tokens_before=8000, tokens_after=3000)
        self.assertEqual([r[0] for r in self._records()], [1, 2])

    def test_spilled_output_row_describes_the_file_that_was_written(self) -> None:
        body = "输出内容" * 50
        path = os.path.join(self.dir, "spill", "one.txt")
        self.store.append_spilled_output(
            session_id=self.sid, path=path, content=body,
            preview_head=body[:200], preview_tail=body[-200:])

        row = self.store._conn.execute(
            "SELECT path, bytes, sha256, preview_head FROM spilled_outputs WHERE session_id = ?",
            (self.sid,)).fetchone()
        self.assertEqual(row[0], path)
        self.assertEqual(row[1], len(body.encode("utf-8")))
        self.assertEqual(row[2], hashlib.sha256(body.encode("utf-8")).hexdigest())
        self.assertEqual(row[3], body[:200])

    def test_context_state_lands_on_the_session_row(self) -> None:
        self.store.set_context_state(self.sid, context_length=3100, overflow_attempts=1)

        row = self.store._conn.execute(
            "SELECT context_length, overflow_attempts FROM sessions WHERE id = ?",
            (self.sid,)).fetchone()
        self.assertEqual((row[0], row[1]), (3100, 1))

    def test_context_state_ignores_nothing_to_update(self) -> None:
        self.store.set_context_state(self.sid)  # must not raise or wipe values
        row = self.store._conn.execute(
            "SELECT context_length FROM sessions WHERE id = ?", (self.sid,)).fetchone()
        self.assertIsNotNone(row[0])


class HookTest(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.mkdtemp(prefix="desk-hooks-")
        self.seen: list[dict] = []
        self.spills: list[tuple[str, str]] = []
        self.gov = context_mechanism.ContextGovernor(
            128000,
            spill_dir=self.dir,
            on_compacted=self.seen.append,
            on_spilled=lambda path, content: self.spills.append((path, content)),
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_compaction_event_carries_generation_and_summary(self) -> None:
        self.gov.generation = 3
        self.gov._push_event("L2", 9000, 3000, 1, "pressure", summary="摘要正文")

        data = self.seen[0]
        self.assertEqual(data["level"], "L2")
        self.assertEqual(data["generation"], 3)
        self.assertEqual(data["summaryText"], "摘要正文")
        self.assertEqual((data["estimatedTokens"], data["tokensAfter"]), (9000, 3000))

    def test_spill_hook_receives_the_bytes_that_went_to_disk(self) -> None:
        body = "很长的一段工具输出" * 100
        locator, path = self.gov.spill_store.spill(body)

        self.assertTrue(path)
        self.assertIn("path=", locator)
        self.assertEqual(self.spills, [(path, body)])
        with open(path, "r", encoding="utf-8") as f:
            self.assertEqual(f.read(), body)

    def test_a_failing_hook_cannot_break_the_compaction(self) -> None:
        def boom(_data):
            raise RuntimeError("store down")

        gov = context_mechanism.ContextGovernor(128000, spill_dir=self.dir, on_compacted=boom)
        gov._push_event("L0|L1", 9000, 8000, 0, "pressure")  # must not raise

        self.assertEqual(len(gov.events), 1)


if __name__ == "__main__":
    unittest.main()