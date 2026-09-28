"""Sidecar wiring for memory: prompt assembly, turn-boundary upkeep, helpers.

The hooks themselves are tested in tests/test_memory_hooks.py; this file pins
the sidecar side -- that memory text reaches the prompt at the volatile end,
that the turn helpers pick the right messages, and that a missing or broken
engine can never break a chat.
"""
from __future__ import annotations

import os
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "python"))

import prompt_build  # noqa: E402
import sidecar  # noqa: E402


class FakeHooks:
    def __init__(self, text=None, boom=False):
        self._text = text
        self._boom = boom
        self.writes: list[tuple] = []
        self.prefetches: list[str] = []
        self.reaps = 0

    def injection(self):
        if self._boom:
            raise RuntimeError("engine blew up")
        return self._text

    def note_turn(self, user_text, assistant_text=""):
        self.writes.append((user_text, assistant_text))

    def start_prefetch(self, query):
        self.prefetches.append(query)

    def maybe_reap(self):
        self.reaps += 1
        return True


class WiringCase(unittest.TestCase):
    def setUp(self) -> None:
        self._saved_hooks = sidecar._MEMORY_HOOKS
        self._saved_ready = sidecar._MEMORY_READY

    def tearDown(self) -> None:
        sidecar._MEMORY_HOOKS = self._saved_hooks
        sidecar._MEMORY_READY = self._saved_ready


class HelpersTest(WiringCase):
    def test_last_user_and_reply_text(self) -> None:
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "第一问"},
            {"role": "assistant", "content": "第一答"},
            {"role": "user", "content": "第二问"},
            {"role": "assistant", "content": "第二答"},
        ]
        self.assertEqual(sidecar._last_user_text(messages), "第二问")
        self.assertEqual(sidecar._last_reply_text(messages), "第二答")

    def test_helpers_tolerate_missing_or_odd_messages(self) -> None:
        self.assertEqual(sidecar._last_user_text(None), "")
        self.assertEqual(sidecar._last_user_text([]), "")
        self.assertEqual(sidecar._last_user_text([{"role": "user", "content": None}]), "")
        self.assertEqual(sidecar._last_reply_text([{"role": "user", "content": "x"}]), "")


class MemoryTextTest(WiringCase):
    def test_returns_the_snapshot_when_hooks_are_ready(self) -> None:
        sidecar._MEMORY_HOOKS = FakeHooks("## 相关记忆（Mnemosyne）\n- 示例用户")
        sidecar._MEMORY_READY = True
        self.assertIn("示例用户", sidecar._memory_text())

    def test_no_hooks_means_no_memory_block(self) -> None:
        sidecar._MEMORY_HOOKS = None
        sidecar._MEMORY_READY = True
        self.assertIsNone(sidecar._memory_text())

    def test_broken_engine_degrades_to_no_memory(self) -> None:
        sidecar._MEMORY_HOOKS = FakeHooks(boom=True)
        sidecar._MEMORY_READY = True
        self.assertIsNone(sidecar._memory_text())

    def test_hooks_lookup_never_raises(self) -> None:
        sidecar._MEMORY_HOOKS = None
        sidecar._MEMORY_READY = None          # force a fresh decision
        hooks = sidecar._memory_hooks()        # mnemosyne may or may not exist
        self.assertTrue(hooks is None or hasattr(hooks, "note_turn"))
        # The decision is cached so a broken install is not retried per chat.
        self.assertIsNotNone(sidecar._MEMORY_READY)


class PromptAssemblyTest(WiringCase):
    def _prompt(self, memory=None):
        return prompt_build.build(
            workspace="E:/ws", memory=memory,
            now=prompt_build._dt.datetime(2026, 9, 24, 10, 0))

    def test_memory_renders_before_the_runtime_block(self) -> None:
        got = self._prompt("## 相关记忆（Mnemosyne）\n- 上周定了用 vLLM")
        self.assertIn("上周定了用 vLLM", got)
        self.assertLess(got.index("相关记忆"), got.index("## 运行环境"))

    def test_no_memory_means_no_section(self) -> None:
        self.assertNotIn("相关记忆", self._prompt())
        self.assertNotIn("相关记忆", self._prompt("   "))
        self.assertEqual(self._prompt(), self._prompt(None))


class StaticWiringTest(WiringCase):
    def setUp(self) -> None:
        super().setUp()
        with open(os.path.join(REPO, "python", "sidecar.py"), encoding="utf-8") as fh:
            self.src = fh.read()

    def test_turn_boundary_calls_are_wired(self) -> None:
        self.assertIn("hooks.note_turn(question, _last_reply_text(ctx.messages))", self.src)
        self.assertIn("hooks.start_prefetch(question)", self.src)
        self.assertIn("hooks.maybe_reap()", self.src)

    def test_prompt_assembly_receives_memory(self) -> None:
        self.assertIn("memory=_memory_text()", self.src)

    def test_engine_is_optional(self) -> None:
        self.assertIn('find_spec("mnemosyne")', self.src)


if __name__ == "__main__":
    unittest.main()