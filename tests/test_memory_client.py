"""Mnemosyne engine client: handshake, tool calls, degradation.

The engine is exercised through a fake process so these tests stay offline and
do not need the mnemosyne package installed. What matters here is the wire
behaviour: the MCP handshake happens once, tool calls carry the right names and
arguments, and a broken engine degrades instead of raising into the chat.
"""
from __future__ import annotations

import json
import os
import sys
import unittest
from unittest import mock

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "python"))

from memory import memory_client as mc  # noqa: E402
from memory import memory_config  # noqa: E402


def _text_result(payload) -> str:
    return json.dumps({"jsonrpc": "2.0", "id": 99,
                       "result": {"content": [{"type": "text",
                                               "text": json.dumps(payload)}]}})


class FakeStdin:
    def __init__(self, sink):
        self._sink = sink

    def write(self, text):
        self._sink.append(text)

    def flush(self):
        pass


class FakeProc:
    """Behaves like subprocess.Popen well enough for the reader thread."""

    def __init__(self, replies, *, alive=True):
        self.written: list[str] = []
        self.stdin = FakeStdin(self.written)
        self.stdout = iter([r + "\n" for r in replies] + [""])
        self._alive = alive
        self.killed = False

    def poll(self):
        return None if self._alive else 1

    def kill(self):
        self._alive = False
        self.killed = True


class ChildPidTest(unittest.TestCase):
    """child_pid: the PID while up, None once the child is disposed.

    Real-machine smoke tests poll this property to prove the OS actually got
    the memory back, so a wrong answer here would quietly invalidate that
    check. Built on the fakes, no subprocess involved.
    """

    def _engine(self):
        return mc.MemoryEngine(python_exe=sys.executable, home="C:/nonexistent")

    def _fake_child(self, pid=None, *, alive=True):
        proc = FakeProc([], alive=alive)
        if pid is not None:
            proc.pid = pid
        return mc.ChildProcess(proc), proc

    def test_pid_is_none_before_start(self) -> None:
        self.assertIsNone(self._engine().child_pid)

    def test_pid_is_reported_while_the_child_is_held(self) -> None:
        eng = self._engine()
        child, _proc = self._fake_child(pid=4242)
        eng._proc._child = child
        self.assertEqual(eng.child_pid, 4242)

    def test_pid_clears_once_close_kills_the_child(self) -> None:
        eng = self._engine()
        child, proc = self._fake_child(pid=4242)
        eng._proc._child = child
        eng.close()
        self.assertIsNone(eng.child_pid)
        self.assertTrue(proc.killed, "close() has to kill, not just forget")

    def test_a_dead_child_reports_no_pid(self) -> None:
        eng = self._engine()
        child, _proc = self._fake_child(pid=4242, alive=False)
        eng._proc._child = child
        self.assertEqual(eng.child_pid, 4242, "the PID is read off the handle")


class ClientCase(unittest.TestCase):
    replies: list[str] = []

    def setUp(self) -> None:
        self.procs: list[FakeProc] = []
        self._real_popen = mc.subprocess.Popen
        self._real_assert_safe = memory_config.assert_safe

        def fake_popen(*args, **kwargs):
            proc = FakeProc(list(self.replies))
            self.procs.append(proc)
            return proc

        mc.subprocess.Popen = fake_popen          # type: ignore[assignment]
        self.engine = mc.MemoryEngine(base_env={}, python_exe="python3")

    def tearDown(self) -> None:
        mc.subprocess.Popen = self._real_popen    # type: ignore[assignment]
        memory_config.assert_safe = self._real_assert_safe  # type: ignore[assignment]

    def sent(self, index: int = -1) -> list[dict]:
        raw = self.procs[index].written
        return [json.loads(line) for line in raw if line.strip()]


class HandshakeTest(ClientCase):
    replies = [json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "x"}})]

    def test_start_initializes_then_notifies(self) -> None:
        ok, detail = self.engine.start()
        self.assertTrue(ok, detail)
        frames = self.sent()
        self.assertEqual(frames[0]["method"], "initialize")
        self.assertIn("clientInfo", frames[0]["params"])
        self.assertEqual(frames[1]["method"], "notifications/initialized")
        self.assertNotIn("id", frames[1])

    def test_second_start_does_not_rehandshake(self) -> None:
        self.engine.start()
        before = len(self.sent())
        self.engine.start()
        self.assertEqual(len(self.sent()), before)


class ToolCallTest(ClientCase):
    replies = [
        json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}}),
        _text_result({"slots": [{"category": "identity", "name": "称呼",
                                 "body": "示例用户"}]}),
        _text_result({"results": [{"content": "上周定了用 vLLM"}]}),
        _text_result({"ok": True}),
    ]

    def test_canonical_all_lists_every_slot(self) -> None:
        self.engine.start()
        slots = self.engine.canonical_all()
        self.assertEqual(slots[0]["body"], "示例用户")
        call = self.sent()[2]
        self.assertEqual(call["method"], "tools/call")
        self.assertEqual(call["params"]["name"], "mnemosyne_recall_canonical")
        self.assertEqual(call["params"]["arguments"], {})

    def test_recall_returns_the_result_list(self) -> None:
        self.engine.start()
        self.engine.canonical_all()
        hits = self.engine.recall("用的什么推理框架", limit=3)
        self.assertEqual(hits, [{"content": "上周定了用 vLLM"}])
        args = self.sent()[-1]["params"]["arguments"]
        self.assertEqual(args["limit"], 3)

    def test_remember_defaults_to_global_scope(self) -> None:
        self.engine.start()
        self.engine.canonical_all()
        self.engine.recall("x")
        self.engine.remember("用户叫示例用户", source="preference")
        args = self.sent()[-1]["params"]["arguments"]
        self.assertEqual(args["content"], "用户叫示例用户")
        self.assertEqual(args["source"], "preference")
        # Recalled facts are cross-session by design; session scope would hide
        # them from the next conversation.
        self.assertEqual(args["scope"], "global")

    def test_canonical_set_requires_category_name_body(self) -> None:
        self.engine.start()
        self.engine.canonical_all()
        self.engine.recall("x")
        self.engine.canonical_set("identity", "称呼", "示例用户")
        call = self.sent()[-1]
        self.assertEqual(call["params"]["name"], "mnemosyne_remember_canonical")
        self.assertEqual(call["params"]["arguments"],
                         {"category": "identity", "name": "称呼", "body": "示例用户",
                          "source": ""})


class DegradationTest(ClientCase):
    replies = [json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}})]

    def test_spawn_failure_degrades_instead_of_raising(self) -> None:
        def boom(*args, **kwargs):
            raise OSError("no mnemosyne on this machine")

        mc.subprocess.Popen = boom                 # type: ignore[assignment]
        ok, detail = self.engine.start()
        self.assertFalse(ok)
        # The detail carries the reason; "this engine is unavailable" is the
        # state the caller reads off the object, not the message text.
        self.assertIn("no mnemosyne on this machine", detail)
        self.assertTrue(self.engine.is_unavailable)
        # A second attempt must not raise either.
        self.assertFalse(self.engine.start()[0])

    def test_safety_assertions_block_the_spawn(self) -> None:
        memory_config.assert_safe = lambda env: ["llm_disabled"]  # type: ignore[assignment]
        ok, detail = self.engine.start()
        self.assertFalse(ok)
        self.assertIn("refused to start", detail)
        self.assertEqual(self.procs, [])           # never spawned

    def test_idle_reap_kills_the_child(self) -> None:
        now = [1000.0]
        engine = mc.MemoryEngine(base_env={}, python_exe="python3",
                                 clock=lambda: now[0], idle_threshold=60)
        self.assertTrue(engine.start()[0], "handshake reply was scripted")
        self.assertFalse(engine.maybe_reap())      # just used, not idle yet
        now[0] += 120
        self.assertTrue(engine.maybe_reap())
        self.assertTrue(self.procs[-1].killed)


class ConsolidateTest(unittest.TestCase):
    """Consolidation is what gives long-term recall its vectors.

    Without it episodic rows stay unembedded and recall quietly degrades to
    keywords -- plausible-looking hits, no semantic search. It is a CLI verb
    (`mnemosyne sleep`), so it runs as a one-shot child, and it must never
    break shutdown.
    """

    def _engine(self, *, exists=True):
        eng = mc.MemoryEngine(python_exe=os.path.join("C:\\py", "python.exe"),
                              home="C:/nonexistent")
        return eng

    def test_uses_the_cli_next_to_its_own_interpreter(self) -> None:
        eng = self._engine()
        seen = {}
        def fake_run(cmd, **kw):
            seen["cmd"] = cmd
            seen["env"] = kw.get("env") or {}
            return type("P", (), {"returncode": 0, "stdout": "Consolidation complete", "stderr": ""})()
        with mock.patch.object(mc.subprocess, "run", fake_run), \
             mock.patch.object(mc.os.path, "exists", lambda p: True):
            out = eng.consolidate()
        self.assertEqual(out["status"], "ok")
        self.assertEqual(seen["cmd"][1], mc.CONSOLIDATE_SUBCOMMAND)
        self.assertIn("mnemosyne", seen["cmd"][0])
        self.assertEqual(seen["env"].get(mc.memory_config.DATA_DIR_KEY), "C:/nonexistent")

    def test_a_missing_cli_is_reported_not_raised(self) -> None:
        eng = self._engine()
        with mock.patch.object(mc.os.path, "exists", lambda p: False):
            out = eng.consolidate()
        self.assertEqual(out["status"], "error")
        self.assertIn("no mnemosyne CLI", out["detail"])

    def test_close_consolidates_only_after_a_write(self) -> None:
        eng = self._engine()
        calls = []
        eng.consolidate = lambda: calls.append(1) or {"status": "ok"}
        eng._proc.shutdown = lambda: calls.append("kill")
        eng.close()
        self.assertEqual(calls, ["kill"], "a read-only session must not pay for a pass")

        eng = self._engine()
        calls = []
        eng.consolidate = lambda: calls.append("sleep") or {"status": "ok"}
        eng._proc.shutdown = lambda: calls.append("kill")
        eng._dirty = True
        eng.close()
        self.assertEqual(calls, ["sleep", "kill"])

    def test_a_failing_pass_still_shuts_the_child_down(self) -> None:
        eng = self._engine()
        calls = []
        def boom():
            calls.append("boom")
            raise RuntimeError("engine exploded")
        eng.consolidate = boom
        eng._proc.shutdown = lambda: calls.append("kill")
        eng._dirty = True
        eng.close()
        self.assertEqual(calls, ["boom", "kill"])


if __name__ == "__main__":
    unittest.main()