"""The L2 summarizer must repeat the main session's tools array.

Providers such as MiniMax build the cached prefix as
"tool definitions -> system prompt -> history", so a summary request that
omits tools diverges from the main session at byte zero and never hits the
cache. These tests pin that the summarizer forwards the same array the loop
is sending this round, and that it stays a no-op when the session has no
tools (so the request shape is unchanged in that case).
"""
from __future__ import annotations

import os
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "python"))

import llm  # noqa: E402
import sidecar  # noqa: E402

TOOLS = [{"type": "function",
          "function": {"name": "read_file", "description": "Read a file",
                       "parameters": {"type": "object", "properties": {}}}}]


class SummarizerToolsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.captured: dict = {}
        self._orig_fn = llm.chat_messages_once
        self._orig_tools = getattr(sidecar._CHAT_TLS, "tools", None)

        def fake_messages_once(cfg, messages, **kw):
            self.captured["messages"] = list(messages)
            self.captured["kw"] = dict(kw)
            return "SUMMARY"

        llm.chat_messages_once = fake_messages_once

    def tearDown(self) -> None:
        llm.chat_messages_once = self._orig_fn
        sidecar._CHAT_TLS.tools = self._orig_tools

    def _summarize_once(self):
        """Run the production summarizer with a cache-sharing payload."""
        fn = sidecar._make_summarizer(None)
        return fn([{"role": "system", "content": "sys"},
                   {"role": "user", "content": "hi"},
                   {"role": "user", "content": "summarize the above"}])

    def test_tools_repeated_when_session_uses_tools(self) -> None:
        sidecar._CHAT_TLS.tools = TOOLS
        self.assertEqual(self._summarize_once(), "SUMMARY")
        self.assertEqual(self.captured["kw"].get("tools"), TOOLS)

    def test_no_tools_kwarg_when_session_has_none(self) -> None:
        sidecar._CHAT_TLS.tools = None
        self._summarize_once()
        self.assertNotIn("tools", self.captured["kw"])

    def test_legacy_string_payload_keeps_cold_path(self) -> None:
        """String payloads still go out as the old single cold prompt."""
        called: dict = {}
        self._orig_prompt = llm.chat_once

        def fake_chat_once(cfg, prompt, **kw):
            called["prompt"] = prompt
            return "S"

        llm.chat_once = fake_chat_once
        try:
            sidecar._CHAT_TLS.tools = TOOLS
            self.assertEqual(sidecar._make_summarizer(None)("middle text"), "S")
        finally:
            llm.chat_once = self._orig_prompt
        self.assertIn("middle text", called["prompt"])
        self.assertEqual(self.captured, {})  # never reached the list path

    def test_run_chat_publishes_the_round_tools(self) -> None:
        """Guard: the chat handler must publish this round's schema array."""
        src = open(os.path.join(REPO, "python", "sidecar.py"), encoding="utf-8").read()
        self.assertIn("_CHAT_TLS.tools = deps.tool_schemas() if ctx.use_tools else None", src)


if __name__ == "__main__":
    unittest.main()