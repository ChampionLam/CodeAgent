"""Subagent engine unit tests (design: docs/2026-09-25-subagent-design.md).

Covers: bounded concurrency, immediate dispatch, consolidation into one
message, degradation, memory gate, post-batch cleanup (registry / threads /
single gc), interruption with partial text, and the depth=1 tool fence.
Injected fakes only - no real model, no host-derived thresholds.
"""

from __future__ import annotations

import gc
import os
import shutil
import sys
import tempfile
import threading
import time
import tracemalloc
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "python"))

import subagent as sa  # noqa: E402


def _mk_task(goal: str) -> dict:
    return {"goal": goal, "context": "ctx for %s" % goal}


class _Recorder:
    """Collects engine events for assertions."""

    def __init__(self):
        self.events: list[tuple[str, dict]] = []
        self.lock = threading.Lock()

    def __call__(self, event: str, payload: dict) -> None:
        with self.lock:
            self.events.append((event, dict(payload)))

    def of(self, event: str) -> list[dict]:
        with self.lock:
            return [p for name, p in self.events if name == event]


def _ok_node(task: dict, node_ctx: dict) -> sa.NodeResult:
    return sa.NodeResult(
        node_id=node_ctx["node_id"], goal=node_ctx["goal"], status="done",
        summary="summary of %s" % node_ctx["goal"],
        usage={"input_tokens": 10, "output_tokens": 20},
    )


class SubagentTestCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="subagent-test-")
        self.recorder = _Recorder()
        self.collect_calls: list[int] = []

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _delegator(self, run_node, **kw):
        kw.setdefault("emit", self.recorder)
        kw.setdefault("transcripts_dir", self.dir)
        kw.setdefault("collect", lambda: self.collect_calls.append(1))
        return sa.Delegator(run_node, **kw)

    def _wait_event(self, event: str, timeout: float = 3.0) -> list[dict]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            found = self.recorder.of(event)
            if found:
                return found
            time.sleep(0.01)
        return self.recorder.of(event)

    def _drain_threads(self, timeout: float = 10.0) -> int:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not any(t.name.startswith("subagent-worker-")
                       and t.is_alive()
                       for t in threading.enumerate()):
                break
            time.sleep(0.02)
        return sum(1 for t in threading.enumerate()
                   if t.name.startswith("subagent-worker-") and t.is_alive())


class TestConcurrency(SubagentTestCase):
    def test_three_nodes_run_in_parallel(self):
        overlap = {"now": 0, "max": 0}
        lock = threading.Lock()
        gate = threading.Event()

        def node(task, ctx):
            with lock:
                overlap["now"] += 1
                overlap["max"] = max(overlap["max"], overlap["now"])
            gate.wait(timeout=2.0)
            with lock:
                overlap["now"] -= 1
            return sa.NodeResult(node_id=ctx["node_id"], goal=ctx["goal"],
                                 status="done", summary="ok")

        d = self._delegator(node, limit=3)
        batch = d.dispatch([_mk_task("g%d" % i) for i in range(3)], "s-1")
        # Let the workers hit the gate simultaneously.
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and overlap["now"] < 3:
            time.sleep(0.02)
        self.assertEqual(len(d.active()), 3, "3 nodes should be running")
        self.assertLessEqual(overlap["max"], 3)
        gate.set()
        d.join(batch.batch_id, timeout=5)
        self.assertGreaterEqual(overlap["max"], 1)
        self.assertEqual(batch.status, "done")

    def test_four_tasks_rejected(self):
        d = self._delegator(_ok_node, limit=3)
        with self.assertRaises(sa.DelegationError) as cm:
            d.dispatch([_mk_task("g%d" % i) for i in range(4)], "s-1")
        self.assertEqual(cm.exception.code, "TOO_MANY_TASKS")
        self.assertEqual(d.active(), [], "rejected batch leaves nothing running")

    def test_limit_cap_is_three_even_when_override_higher(self):
        d = self._delegator(_ok_node, limit=99)
        self.assertEqual(d.batch("nope"), None)
        # Hard cap lives in the constants; the delegator itself was told 99,
        # but effective_limit never exceeds the cap.
        limit, _ = sa.effective_limit(memory_available=16 * 1024**3,
                                      cpu_count=32)
        self.assertEqual(limit, sa.MAX_CONCURRENT_HARD_CAP)
        self.assertEqual(limit, 3)


class TestImmediateReturn(SubagentTestCase):
    def test_dispatch_returns_immediately(self):
        started = threading.Event()
        release = threading.Event()

        def slow_node(task, ctx):
            started.set()
            release.wait(timeout=5.0)
            return sa.NodeResult(node_id=ctx["node_id"], goal=ctx["goal"],
                                 status="done", summary="slow but fine")

        d = self._delegator(slow_node, limit=3)
        t0 = time.perf_counter()
        batch = d.dispatch([_mk_task("slow-%d" % i) for i in range(3)], "s-1")
        elapsed = time.perf_counter() - t0
        self.assertLess(elapsed, 0.3,
                        "dispatch must return in < 0.3s, took %.3fs" % elapsed)
        self.assertTrue(started.wait(timeout=2.0), "worker should have started")
        self.assertEqual(len(d.active()), 3, "dispatch must not block")
        release.set()
        d.join(batch.batch_id, timeout=5)


class TestConsolidation(SubagentTestCase):
    def test_three_done_nodes_merge_into_one_message(self):
        d = self._delegator(_ok_node, limit=3)
        batch = d.dispatch([_mk_task("goal-a"), _mk_task("goal-b"),
                            _mk_task("goal-c")], "s-1")
        d.join(batch.batch_id, timeout=5)
        msg = d.consolidated_message(batch.batch_id)
        self.assertIn("sa-1", msg)
        self.assertIn("sa-2", msg)
        self.assertIn("sa-3", msg)
        self.assertIn("summary of goal-a", msg)
        self.assertIn("summary of goal-b", msg)
        self.assertIn("summary of goal-c", msg)
        self.assertNotIn("degraded", msg.lower())

    def test_failed_node_is_reported_honestly(self):
        def flaky(task, ctx):
            if ctx["node_id"] == "sa-2":
                raise RuntimeError("child exploded")
            return sa.NodeResult(node_id=ctx["node_id"], goal=ctx["goal"],
                                 status="done", summary="fine")

        d = self._delegator(flaky, limit=3)
        batch = d.dispatch([_mk_task("a"), _mk_task("b"), _mk_task("c")], "s-1")
        d.join(batch.batch_id, timeout=5)
        msg = d.consolidated_message(batch.batch_id)
        self.assertIn("sa-2", msg)
        self.assertIn("interrupted", msg, "failure must be visible, not success")
        self.assertIn("child exploded", msg)
        self.assertIn("sa-1", msg)
        self.assertIn("sa-3", msg)
        # still one consolidated string, not per-node messages: the two
        # successful nodes each contribute one summary line, the failed
        # node contributes its error instead.
        self.assertEqual(msg.count("fine"), 2)
        self.assertEqual(batch.status, "partial")

    def test_partial_flag_when_not_all_done(self):
        def half(task, ctx):
            if ctx["node_id"] == "sa-3":
                raise ValueError("boom")
            return sa.NodeResult(node_id=ctx["node_id"], goal=ctx["goal"],
                                 status="done", summary="ok")

        d = self._delegator(half, limit=3)
        batch = d.dispatch([_mk_task("x"), _mk_task("y"), _mk_task("z")], "s-1")
        d.join(batch.batch_id, timeout=5)
        self.assertEqual(batch.status, "partial")


class TestDegradation(SubagentTestCase):
    def test_low_memory_and_cpu_degrade_to_one_node(self):
        orig_avail, orig_cpu = sa.available_memory_bytes, os.cpu_count
        sa.available_memory_bytes = lambda: 3 * 1024**3
        os.cpu_count = lambda: 2
        try:
            d = self._delegator(_ok_node)  # no explicit limit -> probe path
            batch = d.dispatch([_mk_task("only-one")], "s-1")
            d.join(batch.batch_id, timeout=5)
            self.assertEqual(batch.limit, 1)
            self.assertTrue(batch.degraded)
            self.assertIn("3.0", batch.limit_reason,
                          "reason must carry the concrete memory number")
            self.assertIn("2 cpus", batch.limit_reason)
            msg = d.consolidated_message(batch.batch_id)
            self.assertIn("降级运行", msg)
            self.assertIn("3.0GB", msg)
            # Two tasks at limit=1 must be refused outright.
            with self.assertRaises(sa.DelegationError) as cm:
                d.dispatch([_mk_task("a"), _mk_task("b")], "s-2")
            self.assertEqual(cm.exception.code, "TOO_MANY_TASKS")
        finally:
            sa.available_memory_bytes = orig_avail
            os.cpu_count = orig_cpu

    def test_effective_limit_tiers(self):
        # (memory, cpu, expected limit)
        cases = [
            (3 * 1024**3, 8, 1),      # <4GB -> 1
            (5 * 1024**3, 8, 2),      # 4-8GB -> 2
            (16 * 1024**3, 8, 3),     # >=8GB -> 3, cpu 8 -> 4 -> cap 3
            (16 * 1024**3, 2, 1),     # 2 cpus -> 1
            (16 * 1024**3, 4, 2),     # 4 cpus -> 2
        ]
        for mem, cpu, expect in cases:
            limit, reason = sa.effective_limit(memory_available=mem,
                                               cpu_count=cpu)
            self.assertEqual(limit, expect, "mem=%r cpu=%r" % (mem, cpu))
            self.assertTrue(reason, "reason must never be empty")

    def test_unknown_probes_assume_full_cap(self):
        orig_avail, orig_cpu = sa.available_memory_bytes, os.cpu_count
        sa.available_memory_bytes = lambda: None
        os.cpu_count = lambda: None
        try:
            limit, reason = sa.effective_limit()
            self.assertEqual(limit, 3)
            self.assertIn("unknown", reason)
        finally:
            sa.available_memory_bytes = orig_avail
            os.cpu_count = orig_cpu


class TestMemoryGate(SubagentTestCase):
    def test_high_rss_refuses_dispatch(self):
        d = self._delegator(_ok_node, limit=3, rss=lambda: 600 * 1024 * 1024)
        with self.assertRaises(sa.DelegationError) as cm:
            d.dispatch([_mk_task("a")], "s-1")
        self.assertEqual(cm.exception.code, "MEMORY_GATE")
        self.assertIn("600", cm.exception.message)
        self.assertEqual(d.active(), [])


class TestCleanup(SubagentTestCase):
    def test_no_residue_after_batch(self):
        d = self._delegator(_ok_node, limit=3)
        baseline_threads = threading.active_count()
        batch = d.dispatch([_mk_task("c%d" % i) for i in range(3)], "s-1")
        d.join(batch.batch_id, timeout=5)
        # Let the worker threads fully exit their frames.
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and threading.active_count() > baseline_threads:
            time.sleep(0.02)
        self.assertEqual(d.active(), [],
                         "node registry must be drained after the batch")
        self.assertEqual(threading.active_count(), baseline_threads,
                         "threads must return to the baseline count")
        self.assertEqual(len(self.collect_calls), 1,
                         "gc must run exactly once at the batch boundary")
        self.assertIsNotNone(batch.rss_before)
        self.assertIsNotNone(batch.rss_peak)

    def test_collect_runs_once_even_with_failures(self):
        def boom(task, ctx):
            raise RuntimeError("child dies")

        d = self._delegator(boom, limit=3)
        batch = d.dispatch([_mk_task("f%d" % i) for i in range(3)], "s-1")
        d.join(batch.batch_id, timeout=5)
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and self._drain_threads() > 0:
            time.sleep(0.02)
        self.assertEqual(len(self.collect_calls), 1)
        self.assertEqual(d.active(), [])

    def test_single_gc_across_multiple_batches(self):
        d = self._delegator(_ok_node, limit=3)
        for i in range(3):
            batch = d.dispatch([_mk_task("batch-%d" % i)], "s-%d" % i)
            d.join(batch.batch_id, timeout=5)
        self.assertEqual(len(self.collect_calls), 3,
                         "one collect per batch boundary, three batches")
        self.assertEqual(d.active(), [])


class _History:
    """Stand-in for a child agent's message history / stream buffer."""

    def __init__(self, size: int):
        self.payload = bytearray(size)


class TestMemoryDiscipline(SubagentTestCase):
    def test_no_message_history_retained_after_batch(self):
        import weakref
        refs: list[weakref.ref] = []
        keepalive: list[_History] = []   # released once the workers start

        def chatty(task, ctx):
            history = _History(4 * 1024 * 1024)  # 4 MB "message history"
            refs.append(weakref.ref(history))
            keepalive.append(history)
            # Deltas and tool output go to the transcript (disk), never
            # into a retained in-memory stream.
            ctx["emit"]("subagent.delta", {"text": "z" * 65536})
            ctx["emit"]("subagent.tool", {"name": "fetch", "args": {}})
            return sa.NodeResult(node_id=ctx["node_id"], goal=ctx["goal"],
                                 status="done",
                                 summary="kept summary only")

        d = self._delegator(chatty, limit=3)
        batch = d.dispatch([_mk_task("m%d" % i) for i in range(3)], "s-1")
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and len(refs) < 3:
            time.sleep(0.02)
        keepalive.clear()  # only the child frames (and nothing in the
        # engine) may still reference the histories at this point
        d.join(batch.batch_id, timeout=5)
        self._drain_threads()
        gc.collect()
        self.assertEqual(d.active(), [])
        dead = sum(1 for ref in refs if ref() is None)
        self.assertEqual(dead, len(refs),
                         "the engine must not retain child history objects; "
                         "%d/%d still alive" % (len(refs) - dead, len(refs)))
        for node_id in batch.node_ids:
            result = d._results[batch.batch_id][node_id]
            self.assertEqual(result.status, "done")
            self.assertEqual(result.summary, "kept summary only")
            self.assertLess(len(result.summary), 200)


class TestInterruption(SubagentTestCase):
    def test_interrupted_node_keeps_partial_and_transcript(self):
        def half(task, ctx):
            ctx["emit"]("subagent.delta", {"text": "半截正文" * 20})
            raise KeyboardInterrupt("user stopped the run")

        d = self._delegator(half, limit=3)
        batch = d.dispatch([_mk_task("interrupted-goal")], "s-1")
        d.join(batch.batch_id, timeout=5)
        result = d._results[batch.batch_id]["sa-1"]
        self.assertEqual(result.status, "interrupted")
        self.assertIn("半截正文", result.partial)
        self.assertTrue(result.error)
        self.assertTrue(os.path.exists(result.transcript_path),
                        "transcript file must survive the interruption")
        with open(result.transcript_path, "r", encoding="utf-8") as fh:
            content = fh.read()
        self.assertIn("interrupted", content)
        self.assertIn("半截正文", content)
        msg = d.consolidated_message(batch.batch_id)
        self.assertIn("interrupted", msg)
        self.assertEqual(batch.status, "partial")


class TestForbiddenTools(SubagentTestCase):
    def test_delegate_tool_rejected(self):
        d = self._delegator(_ok_node, limit=3)
        with self.assertRaises(sa.DelegationError) as cm:
            d.dispatch([{"goal": "g", "delegate": "another sub task"}], "s-1")
        self.assertEqual(cm.exception.code, "DISABLED")
        self.assertIn("delegate", cm.exception.message)
        self.assertEqual(d.active(), [])

    def test_delegate_task_and_approval_rejected(self):
        d = self._delegator(_ok_node, limit=3)
        for key in ("delegate_task", "request_approval", "save_rule"):
            with self.assertRaises(sa.DelegationError, msg=key) as cm:
                d.dispatch([{ "goal": "g", key: True}], "s-1")
            self.assertEqual(cm.exception.code, "DISABLED")

    def test_forbidden_list_in_tools_field(self):
        d = self._delegator(_ok_node, limit=3)
        with self.assertRaises(sa.DelegationError) as cm:
            d.dispatch([{"goal": "g",
                         "tools": ["read_file", "delegate", "web_search"]}],
                       "s-1")
        self.assertEqual(cm.exception.code, "DISABLED")

    def test_node_ctx_carries_forbidden_tools(self):
        seen = {}

        def spy(task, ctx):
            seen["forbidden_tools"] = ctx.get("forbidden_tools")
            return sa.NodeResult(node_id=ctx["node_id"], goal=ctx["goal"],
                                 status="done", summary="ok")

        d = self._delegator(spy, limit=3)
        batch = d.dispatch([_mk_task("g")], "s-1")
        d.join(batch.batch_id, timeout=5)
        self.assertIn("delegate", seen["forbidden_tools"])
        self.assertIn("request_approval", seen["forbidden_tools"])


class TestEvents(SubagentTestCase):
    def test_event_sequence(self):
        d = self._delegator(_ok_node, limit=3)
        batch = d.dispatch([_mk_task("ev")], "s-1")
        d.join(batch.batch_id, timeout=5)
        self.assertTrue(self._wait_event("subagent.batch_done"),
                        "batch_done must fire at the batch boundary")
        names = [name for name, _ in self.recorder.events]
        self.assertIn("subagent.start", names)
        self.assertIn("subagent.done", names)
        self.assertIn("subagent.batch_done", names)
        for name, payload in self.recorder.events:
            if name == "subagent.batch_done":
                # batch-level event: carries batch_id + node_ids instead.
                self.assertIn("batch_id", payload)
                self.assertIn("node_ids", payload)
                continue
            self.assertIn("node_id", payload, "%s must carry node_id" % name)
            self.assertIn("batch_id", payload)


class TestTranscriptWriting(SubagentTestCase):
    def test_transcript_exists_per_node(self):
        def chatty(task, ctx):
            ctx["emit"]("subagent.delta", {"text": "line one"})
            ctx["emit"]("subagent.tool", {"name": "read_file"})
            return sa.NodeResult(node_id=ctx["node_id"], goal=ctx["goal"],
                                 status="done", summary="ok")

        d = self._delegator(chatty, limit=3)
        batch = d.dispatch([_mk_task("t%d" % i) for i in range(3)], "s-1")
        d.join(batch.batch_id, timeout=5)
        for node_id in batch.node_ids:
            path = d._results[batch.batch_id][node_id].transcript_path
            self.assertTrue(os.path.exists(path), path)
            with open(path, "r", encoding="utf-8") as fh:
                content = fh.read()
            self.assertIn(node_id, content)
            self.assertIn("line one", content)


class TestRssProbes(unittest.TestCase):
    def test_rss_bytes_returns_int_or_none(self):
        value = sa.rss_bytes()
        self.assertTrue(value is None or (isinstance(value, int) and value > 0))

    def test_available_memory_bytes_returns_int_or_none(self):
        value = sa.available_memory_bytes()
        self.assertTrue(value is None or (isinstance(value, int) and value > 0))


class TestJoinSemantics(SubagentTestCase):
    def test_join_timeout_returns_running_batch(self):
        release = threading.Event()

        def slow(task, ctx):
            release.wait(timeout=5)
            return sa.NodeResult(node_id=ctx["node_id"], goal=ctx["goal"],
                                 status="done", summary="late")

        d = self._delegator(slow, limit=3)
        batch = d.dispatch([_mk_task("slow")], "s-1")
        out = d.join(batch.batch_id, timeout=0.2)
        self.assertEqual(out.batch_id, batch.batch_id)
        self.assertIn(out.status, ("running", "done"))  # timed out, still fine
        release.set()
        d.join(batch.batch_id, timeout=5)
        self.assertEqual(d.batch(batch.batch_id).status, "done")


class TestTracemalloc(unittest.TestCase):
    def test_batch_does_not_hold_child_history(self):
        # A child that allocates a big buffer; after the batch + gc the
        # delegator must not retain it (only summaries survive).
        def alloc_node(task, ctx):
            _ = bytearray(8 * 1024 * 1024)          # 8 MB scratch
            ctx["emit"]("subagent.delta", {"text": "data" * 100})
            return sa.NodeResult(node_id=ctx["node_id"], goal=ctx["goal"],
                                 status="done", summary="small summary")

        directory = tempfile.mkdtemp(prefix="subagent-trace-")
        try:
            collect_calls = []
            d = sa.Delegator(alloc_node, transcripts_dir=directory,
                             limit=3,
                             collect=lambda: collect_calls.append(1))
            tracemalloc.start()
            t0 = tracemalloc.get_traced_memory()[0]
            batch = d.dispatch([_mk_task("tr%d" % i) for i in range(3)], "s-1")
            d.join(batch.batch_id, timeout=10)
            gc.collect()
            current, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            self.assertEqual(d.active(), [])
            self.assertEqual(len(collect_calls), 1)
            # NodeResults + registries are tiny; 8 MB scratch buffers must
            # be gone (peak may be high, current must be low).
            self.assertLess(current - t0, 4 * 1024 * 1024,
                            "batch must not retain child scratch memory: "
                            "delta=%d bytes, peak=%d bytes"
                            % (current - t0, peak))
        finally:
            shutil.rmtree(directory, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
