"""Tests for memory/memory_proc.py: lifecycle with injected fake child,
fake clock and JSON-lines protocol. Zero network, zero real process."""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "python",
    ),
)

from memory import memory_proc as MP  # noqa: E402


class FakeClock:
    """Deterministic clock: only moves when the test ticks it."""

    def __init__(self, start=1000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeChild:
    """Fake subprocess speaking the JSON-lines protocol."""

    def __init__(self, queue=None):
        self.alive = True
        self.written = []
        self.queue = list(queue or [])
        self.timeout = None

    def write_line(self, line: str) -> None:
        if not self.alive:
            raise MP.MemoryProcessDeadError("child died before write")
        self.written.append(line)

    def read_line(self, timeout: float):
        if not self.alive:
            return None
        self.timeout = timeout
        return self.queue.pop(0) if self.queue else None

    def kill(self) -> None:
        self.alive = False


class MemoryProcessTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.spawned = []
        self.killed = []
        spawn = lambda: FakeChild(queue=['{"ok": true}'])  # noqa: E731
        self.spawn = lambda: self.spawned.append(spawn()) or self.spawned[-1]
        self.kill = lambda child: self.killed.append(child)
        self.proc = MP.MemoryProcess(
            self.spawn, clock=self.clock, kill=self.kill)

    # -- case (d): idle reap ------------------------------------------

    def test_not_reaped_before_idle_threshold(self):
        self.proc.ensure_up()
        self.clock.advance(MP.IDLE_THRESHOLD_SECONDS - 1)
        self.assertFalse(self.proc.maybe_reap())
        self.assertTrue(self.proc.is_up)
        self.assertEqual(self.killed, [])

    def test_reaped_after_idle_threshold_once_only(self):
        self.proc.ensure_up()
        self.clock.advance(MP.IDLE_THRESHOLD_SECONDS)
        self.assertTrue(self.proc.maybe_reap())
        self.assertEqual(len(self.killed), 1)
        # Repeated reaps never kill a second time.
        self.assertFalse(self.proc.maybe_reap())
        self.assertFalse(self.proc.maybe_reap())
        self.assertEqual(len(self.killed), 1)

    def test_touch_resets_idle_window(self):
        self.proc.ensure_up()
        self.clock.advance(MP.IDLE_THRESHOLD_SECONDS - 10)
        self.proc.touch()
        self.clock.advance(MP.IDLE_THRESHOLD_SECONDS - 10)
        self.assertFalse(self.proc.maybe_reap())
        self.clock.advance(11)
        self.assertTrue(self.proc.maybe_reap())

    # -- case (e): spawn retry once, then unavailable ------------------

    def test_spawn_failure_retries_once_then_marks_unavailable(self):
        calls = []

        def failing_spawn():
            calls.append(1)
            child = FakeChild()
            child.alive = False  # spawn produced a dead child
            return child

        proc = MP.MemoryProcess(failing_spawn, clock=self.clock,
                                kill=self.kill)
        self.assertFalse(proc.ensure_up())
        self.assertEqual(len(calls), 2)  # one retry
        self.assertTrue(proc.is_unavailable)
        self.assertFalse(proc.is_up)

    def test_unavailable_manager_never_raises_or_retries(self):
        calls = []
        failing = lambda: calls.append(1) or None  # noqa: E731
        proc = MP.MemoryProcess(failing, clock=self.clock, kill=self.kill)
        self.assertFalse(proc.ensure_up())  # attempts 1 and 2
        self.assertTrue(proc.is_unavailable)
        count = len(calls)
        self.assertFalse(proc.ensure_up())  # no further attempts
        self.assertEqual(len(calls), count)
        self.assertTrue(proc.is_unavailable)

    def test_first_spawn_failure_second_succeeds(self):
        calls = []

        def flaky_spawn():
            calls.append(1)
            return None if len(calls) == 1 else FakeChild(queue=['{"ok": 1}'])

        proc = MP.MemoryProcess(flaky_spawn, clock=self.clock,
                                kill=self.kill)
        self.assertTrue(proc.ensure_up())
        self.assertEqual(len(calls), 2)
        self.assertTrue(proc.is_up)
        self.assertFalse(proc.is_unavailable)

    # -- case (f): timeouts and respawn after death --------------------

    def test_request_timeout_raises_custom_error(self):
        self.proc.ensure_up()
        child = self.spawned[0]
        child.queue = []  # never replies
        with self.assertRaises(MP.MemoryTimeoutError):
            self.proc.request({"op": "search"}, timeout=0.1)
        # Child was disposed after the timeout.
        self.assertFalse(self.proc.is_up)

    def test_request_roundtrip(self):
        self.proc.ensure_up()
        child = self.spawned[0]
        child.queue = ['{"reply": "pong"}']
        response = self.proc.request({"op": "ping"}, timeout=1.0)
        self.assertEqual(response, {"reply": "pong"})
        self.assertEqual(child.written, ['{"op": "ping"}'])
        self.assertTrue(self.proc.is_up)

    def test_ensure_up_respawns_after_child_death(self):
        self.proc.ensure_up()
        self.assertEqual(len(self.spawned), 1)
        first = self.spawned[0]
        first.kill()  # external death: not via the manager
        self.assertFalse(self.proc.is_up)
        self.assertTrue(self.proc.ensure_up())
        self.assertEqual(len(self.spawned), 2)  # respawn happened
        self.assertNotEqual(self.spawned[1], first)
        self.assertTrue(self.proc.is_up)

    def test_request_on_dead_child_raises(self):
        with self.assertRaises(MP.MemoryProcessDeadError):
            self.proc.request({"op": "ping"}, timeout=1.0)

    def test_malformed_json_response_raises_and_disposes(self):
        self.proc.ensure_up()
        self.spawned[0].queue = ["not json at all"]
        with self.assertRaises(MP.MemoryProcessError):
            self.proc.request({"op": "ping"}, timeout=1.0)
        self.assertFalse(self.proc.is_up)

    def test_shutdown_disposes_child(self):
        self.proc.ensure_up()
        child = self.spawned[0]
        self.proc.shutdown()
        self.assertEqual(self.killed, [child])
        self.assertFalse(self.proc.is_up)
        # Shutdown is idempotent.
        self.proc.shutdown()
        self.assertEqual(len(self.killed), 1)

    def test_spawn_attempt_count_tracks_every_attempt(self):
        self.proc.ensure_up()
        self.assertEqual(self.proc.spawn_attempt_count, 1)


if __name__ == "__main__":
    unittest.main()
