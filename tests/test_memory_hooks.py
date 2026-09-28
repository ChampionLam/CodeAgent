"""Turn-boundary memory hooks: async write, non-blocking read, degradation.

Three properties are the whole point of this layer, so they are what the tests
pin: a turn never waits for the engine, a not-ready prefetch never delays a
reply, and no engine failure ever reaches the chat.
"""
from __future__ import annotations

import os
import sys
import time
import unittest
from unittest import mock

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "python"))

from memory import memory_config  # noqa: E402
from memory import memory_hooks as hooks_mod  # noqa: E402


class FakeEngine:
    """Records calls; can be made slow or broken per method."""

    def __init__(self, *, canonical=None, hits=None, fail=False,
                 remember_delay=0.0, recall_delay=0.0):
        self.remembered: list[dict] = []
        self.recalls: list[dict] = []
        self.canonical_calls = 0
        self.reaped = 0
        self.closed = False
        self._canonical = canonical or []
        self._hits = hits or []
        self._fail = fail
        self._remember_delay = remember_delay
        self._recall_delay = recall_delay

    def remember(self, content, *, source="user", scope="global"):
        if self._fail:
            raise RuntimeError("engine down")
        time.sleep(self._remember_delay)
        self.remembered.append({"content": content, "source": source, "scope": scope})
        return {"ok": True}

    def recall(self, query, *, limit=5):
        if self._fail:
            raise RuntimeError("engine down")
        self.recalls.append({"query": query, "limit": limit})
        time.sleep(self._recall_delay)
        return list(self._hits)

    def canonical_all(self):
        if self._fail:
            raise RuntimeError("engine down")
        self.canonical_calls += 1
        return list(self._canonical)

    def maybe_reap(self):
        self.reaped += 1
        return True

    def close(self):
        self.closed = True


def _wait_for(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


class WriteSideTest(unittest.TestCase):
    def test_turn_is_persisted_with_global_scope(self) -> None:
        engine = FakeEngine()
        h = hooks_mod.MemoryHooks(engine)
        h.note_turn("上周定了用 vLLM", "记下了")
        h.drain()
        self.assertEqual(len(engine.remembered), 1)
        entry = engine.remembered[0]
        self.assertIn("上周定了用 vLLM", entry["content"])
        # session scope would hide the fact from the next conversation.
        self.assertEqual(entry["scope"], "global")

    def test_note_turn_does_not_block_on_a_slow_engine(self) -> None:
        engine = FakeEngine(remember_delay=0.5)
        h = hooks_mod.MemoryHooks(engine)
        started = time.monotonic()
        h.note_turn("慢引擎", "回复")
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 0.2, "note_turn must return immediately")
        self.assertTrue(_wait_for(lambda: len(engine.remembered) == 1))

    def test_empty_turn_is_not_stored(self) -> None:
        engine = FakeEngine()
        h = hooks_mod.MemoryHooks(engine)
        h.note_turn("   ")
        h.drain()
        self.assertEqual(engine.remembered, [])

    def test_writer_survives_a_failing_engine(self) -> None:
        engine = FakeEngine(fail=True)
        seen: list[str] = []
        h = hooks_mod.MemoryHooks(engine, warn=lambda lvl, msg: seen.append(msg))
        h.note_turn("会失败", "x")           # must not raise
        h.drain()
        self.assertTrue(seen, "a dropped write should be reported")


class ReadSideTest(unittest.TestCase):
    def test_prefetch_feeds_the_next_turn(self) -> None:
        engine = FakeEngine(canonical=[{"category": "identity", "name": "称呼",
                                        "body": "示例用户"}],
                            hits=[{"content": "公司用 8 张 H20"}])
        h = hooks_mod.MemoryHooks(engine)
        self.assertIsNone(h.injection(), "nothing ready before the first prefetch")
        h.start_prefetch("我上次说的部署方案")
        self.assertTrue(_wait_for(lambda: h.injection() is not None))
        text = h.injection()
        self.assertIn("称呼：示例用户", text)
        self.assertIn("公司用 8 张 H20", text)
        self.assertEqual(engine.recalls[0]["query"], "我上次说的部署方案")

    def test_injection_does_not_wait_for_a_running_prefetch(self) -> None:
        engine = FakeEngine(hits=[{"content": "慢命中"}], recall_delay=0.6)
        h = hooks_mod.MemoryHooks(engine)
        h.start_prefetch("慢查询")
        started = time.monotonic()
        value = h.injection()
        self.assertLess(time.monotonic() - started, 0.05,
                        "injection must read a snapshot, never block")
        self.assertIsNone(value)

    def test_second_prefetch_is_skipped_while_one_runs(self) -> None:
        engine = FakeEngine(hits=[{"content": "x"}], recall_delay=0.3)
        h = hooks_mod.MemoryHooks(engine)
        h.start_prefetch("first")
        h.start_prefetch("second")
        self.assertTrue(_wait_for(lambda: len(engine.recalls) > 0))
        time.sleep(0.5)
        self.assertEqual(len(engine.recalls), 1)

    def test_broken_engine_yields_no_memory_and_no_error(self) -> None:
        engine = FakeEngine(fail=True)
        seen: list[str] = []
        h = hooks_mod.MemoryHooks(engine, warn=lambda lvl, msg: seen.append(msg))
        h.start_prefetch("查询")             # must not raise
        self.assertTrue(_wait_for(lambda: bool(seen) or h._prefetching is False))
        self.assertIsNone(h.injection())

    def test_blank_query_does_not_start_a_prefetch(self) -> None:
        engine = FakeEngine()
        h = hooks_mod.MemoryHooks(engine)
        h.start_prefetch("   ")
        time.sleep(0.05)
        self.assertEqual(engine.canonical_calls, 0)


class RenderingTest(unittest.TestCase):
    def test_empty_inputs_render_nothing(self) -> None:
        self.assertIsNone(hooks_mod.render_injection([], []))
        self.assertIsNone(hooks_mod.render_injection([{"body": "  "}], [{"content": ""}]))

    def test_canonical_comes_before_recalled_hits(self) -> None:
        text = hooks_mod.render_injection(
            [{"name": "称呼", "body": "示例用户"}], [{"content": "上次聊了缓存"}])
        self.assertLess(text.index("示例用户"), text.index("上次聊了缓存"))
        self.assertIn(hooks_mod.MEMORY_TITLE, text)

    def test_over_budget_is_truncated_not_dropped(self) -> None:
        huge = [{"content": "丙" * 4000}, {"content": "丁" * 4000}]
        text = hooks_mod.render_injection([], huge)
        self.assertLessEqual(len(text),
                             memory_config.MEMORY_CONTEXT_MAX_CHARS + len(hooks_mod.MEMORY_TITLE) + 200)
        self.assertIn("truncated", text)

    def test_single_item_is_capped(self) -> None:
        text = hooks_mod.render_injection([], [{"content": "戊" * 9000}])
        self.assertLessEqual(len(text), memory_config.MEMORY_CONTEXT_MAX_CHARS + len(hooks_mod.MEMORY_TITLE) + 200)


class HousekeepingTest(unittest.TestCase):
    def test_reap_and_close_delegate(self) -> None:
        engine = FakeEngine()
        h = hooks_mod.MemoryHooks(engine)
        self.assertTrue(h.maybe_reap())
        h.close()
        self.assertEqual(engine.reaped, 1)
        self.assertTrue(engine.closed)

    def test_disabled_hooks_touch_nothing(self) -> None:
        engine = FakeEngine()
        h = hooks_mod.MemoryHooks(engine, enabled=False)
        h.note_turn("不该写", "x")
        h.start_prefetch("不该查")
        h.drain()
        time.sleep(0.05)
        self.assertEqual(engine.remembered, [])
        self.assertEqual(engine.canonical_calls, 0)
        self.assertIsNone(h.injection())


class InjectionKnobTest(unittest.TestCase):
    """The two injection knobs must actually be read.

    The engine ignores MNEMOSYNE_PREFETCH_TOP_K (checked against the installed
    package) and the prompt budget is ours by definition, so both are read on
    this side. A key nothing reads is worse than no key: it looks tunable.
    """

    def test_top_k_defaults_to_the_note_and_follows_the_env(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(memory_config.INJECTION_TOP_K_KEY, None)
            self.assertEqual(hooks_mod.injection_top_k(),
                             int(memory_config.INJECTION_TOP_K_DEFAULT))
            os.environ[memory_config.INJECTION_TOP_K_KEY] = "9"
            self.assertEqual(hooks_mod.injection_top_k(), 9)

    def test_bad_values_fall_back_instead_of_raising(self) -> None:
        with mock.patch.dict(os.environ, {memory_config.INJECTION_TOP_K_KEY: "  "}, clear=False):
            self.assertEqual(hooks_mod.injection_top_k(),
                             int(memory_config.INJECTION_TOP_K_DEFAULT))
        with mock.patch.dict(os.environ, {memory_config.INJECTION_TOP_K_KEY: "many"}, clear=False):
            self.assertEqual(hooks_mod.injection_top_k(),
                             int(memory_config.INJECTION_TOP_K_DEFAULT))

    def test_budget_override_shrinks_the_rendered_block(self) -> None:
        rows = [{"content": "x" * 400} for _ in range(20)]
        with mock.patch.dict(os.environ,
                             {memory_config.INJECTION_TOTAL_CHARS_KEY: "600"}, clear=False):
            block = hooks_mod.render_injection([], rows)
        self.assertIsNotNone(block)
        self.assertLessEqual(len(block), 600)


if __name__ == "__main__":
    unittest.main()