"""L2 cache-sharing summary request tests (contract section 7.8 / Qwen-style).

The L2 mid-section summary used to be a cold single-prompt request: one
fresh user message built from scratch, sharing zero bytes with the main
session, so the provider's automatic prefix cache always missed and the
whole prefix had to be re-prefilled. The new shape reuses the main
session's prefix verbatim:

    [main-session system ...] + [middle messages verbatim] +
    [one appended user summary instruction]

so the request prefix is byte-identical to the main session's and only the
tail differs. These tests pin that contract:

  a) message 0 of the summary request is the main session's system message,
     byte-identical (json round-trip of the exact dict);
  b) the request prefix equals the session prefix message-for-message, and
     the last message is the appended summary instruction;
  c) without a session prefix (or with a legacy string-only summarizer)
     the request falls back to the old flattened-text payload, no error;
  d) static guardrail: the prefix part of the summary request contains no
     per-call dynamic fields (no timestamps) — time-like content, if any,
     may only live in the final instruction message.
"""
from __future__ import annotations

import copy
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "python"))

import context_mechanism as CM  # noqa: E402

# Helper builders are shared with the mechanism suite. Import under both
# layouts: `python -m unittest tests.test_compaction_prefix` (repo root on
# sys.path, tests as a namespace package) and the bare discover mode used
# by /tmp/verify_no_network.py (tests/ itself on sys.path).
try:
    from tests.test_context_mechanism import (  # noqa: E402
        build_chat_session,
        build_tool_session,
    )
except ImportError:  # pragma: no cover - discover mode
    from test_context_mechanism import (  # noqa: E402
        build_chat_session,
        build_tool_session,
    )


class MessageSummarizer:
    """Deterministic stub that opted into the message-list request shape.

    Records every payload it receives; returns a fixed short summary.
    """

    accepts_messages = True

    def __init__(self, out: str = "stub summary of the middle") -> None:
        self.out = out
        self.calls = 0
        self.seen: list = []

    def __call__(self, payload):
        self.calls += 1
        self.seen.append(payload)
        return self.out

    @property
    def last_request(self) -> list:
        payload = self.seen[-1]
        assert isinstance(payload, list), "expected a message-list payload"
        return payload

    @property
    def last_text(self) -> str:
        payload = self.seen[-1]
        assert isinstance(payload, str), "expected a legacy text payload"
        return payload


class CacheSharingRequestTest(unittest.TestCase):
    def _run_l2(self, msgs, stub, window=3000):
        g = CM.ContextGovernor(window, summarizer=stub)
        out, _ = g.ensure_before_dispatch(msgs)
        return out, g

    def test_first_message_is_main_session_system_bytes(self):
        # (a) message 0 is the session's system message, byte-identical
        stub = MessageSummarizer()
        msgs = build_chat_session(n_rounds=20)
        orig_system = copy.deepcopy(msgs[0])
        self._run_l2(msgs, stub)
        request = stub.last_request
        self.assertEqual(request[0].get("role"), "system")
        # byte-identical: canonical json dumps must match exactly
        self.assertEqual(json.dumps(request[0], ensure_ascii=False,
                                    sort_keys=True),
                         json.dumps(orig_system, ensure_ascii=False,
                                    sort_keys=True))
        # and the hash the module itself uses as the hard-boundary judge
        self.assertEqual(CM.system_prompt_bytes([request[0]]),
                         CM.system_prompt_bytes([orig_system]))

    def test_prefix_equals_session_prefix_last_is_instruction(self):
        # (b) request[0:N] == session prefix message-for-message; last
        # message is the appended summary instruction
        stub = MessageSummarizer()
        msgs = build_chat_session(n_rounds=20)
        prefix = CM.session_prefix_for(msgs)
        self.assertIsNotNone(prefix)
        self._run_l2(msgs, stub)
        request = stub.last_request
        n = len(prefix)
        # message-for-message equality against the original session view
        self.assertEqual(request[:n], msgs[:n])
        # the very last message is a user message carrying the instruction
        self.assertEqual(request[-1].get("role"), "user")
        self.assertEqual(request[-1].get("content"), CM.SUMMARY_INSTRUCTION)
        # the instruction appears exactly once, at the tail
        self.assertEqual([i for i, m in enumerate(request)
                          if m.get("content") == CM.SUMMARY_INSTRUCTION],
                         [len(request) - 1])
        # length = prefix + middle + 1 instruction; middle comes from the
        # session between head-end and tail-start (identical dicts)
        head_end = CM._head_protect_end(msgs)
        tail_start = CM._select_tail_protect(
            msgs, 3000)   # window used in _run_l2
        self.assertEqual(len(request), n + (tail_start - head_end) + 1)
        self.assertEqual(request[n:n + (tail_start - head_end)],
                         msgs[head_end:tail_start])

    def test_middle_messages_verbatim_tool_payloads(self):
        # the middle section keeps tool_call ids / arguments byte-identical:
        # a projected or re-serialized middle would break prefix-cache hits.
        # Called via compact_l2 directly so the probe is against the exact
        # messages handed in (no L0/L1 projection in between).
        stub = MessageSummarizer()
        msgs = build_tool_session(n_tools=8, content="A" * 300)
        prefix = CM.session_prefix_for(msgs)
        projected, info = CM.compact_l2(msgs, 3000, stub, prefix)
        request = stub.last_request
        head_end = CM._head_protect_end(msgs)
        tail_start = CM._select_tail_protect(msgs, 3000)
        self.assertGreater(tail_start, head_end + 2)   # real middle exists
        n = len(prefix)
        middle_count = tail_start - head_end
        self.assertEqual(request[:n], msgs[:n])
        self.assertEqual(request[n:n + middle_count],
                         msgs[head_end:tail_start])
        # byte-level probe: tool_call blocks identical under canonical json
        probed = 0
        for orig, got in zip(msgs[head_end:tail_start],
                             request[n:n + middle_count]):
            if orig.get("tool_calls"):
                probed += 1
                self.assertEqual(json.dumps(got.get("tool_calls"),
                                            ensure_ascii=False),
                                 json.dumps(orig.get("tool_calls"),
                                            ensure_ascii=False))
        self.assertGreater(probed, 0)                  # probe really probed
        self.assertEqual(request[-1].get("content"), CM.SUMMARY_INSTRUCTION)

    def test_no_prefix_falls_back_to_legacy_text(self):
        # (c) session without a leading system message: derived prefix is
        # None, nothing injected -> legacy flattened-text payload, no error
        stub = MessageSummarizer()
        msgs = build_chat_session(n_rounds=20)[1:]      # drop the system msg
        self.assertIsNone(CM.session_prefix_for(msgs))
        g = CM.ContextGovernor(3000, summarizer=stub)
        out, _ = g.ensure_before_dispatch(msgs)
        # legacy path: payload is the flattened text, one LLM call, no raise
        self.assertIsInstance(stub.seen[0], str)
        self.assertIn("[user]", stub.last_text)
        self.assertEqual(stub.calls, 1)
        self.assertEqual(g.llm_calls, 1)

    def test_injected_prefix_used_when_view_has_no_system(self):
        # (c bis) no system in the view, but the wiring side (sidecar)
        # injected one: the request still shares the main session's system
        stub = MessageSummarizer()
        system_msg = {"role": "system", "content": "SYS-PROMPT-BYTES"}
        msgs = build_chat_session(n_rounds=20)[1:]
        g = CM.ContextGovernor(3000, summarizer=stub,
                               session_prefix=[system_msg])
        out, _ = g.ensure_before_dispatch(msgs)
        request = stub.last_request
        self.assertEqual(request[0], system_msg)
        self.assertEqual(request[-1].get("content"), CM.SUMMARY_INSTRUCTION)

    def test_legacy_summarizer_keeps_text_payload(self):
        # (c tris) summarizer without accepts_messages: byte-identical old
        # behavior even when a prefix IS available
        class LegacyStub:
            def __init__(self):
                self.calls = 0
                self.seen: list[str] = []

            def __call__(self, text: str) -> str:
                self.calls += 1
                self.seen.append(text)
                return "legacy summary"

        stub = LegacyStub()
        msgs = build_chat_session(n_rounds=20)
        g = CM.ContextGovernor(3000, summarizer=stub)
        out, _ = g.ensure_before_dispatch(msgs)
        self.assertEqual(stub.calls, 1)
        self.assertIsInstance(stub.seen[0], str)
        # old shape: flattened "[role] text" lines, no instruction constant
        self.assertNotIn(CM.SUMMARY_INSTRUCTION, stub.seen[0])
        self.assertIn("[user]", stub.seen[0])

    def test_prefix_part_has_no_dynamic_fields(self):
        # (d) static guardrail: the prefix (everything except the final
        # instruction message) must not contain per-call dynamic fields
        # such as timestamps. Freeze a time-like token into the session
        # prefix would be legal (session content), but the mechanism itself
        # must never inject clock/time values into the request prefix.
        stub = MessageSummarizer()
        msgs = build_chat_session(n_rounds=20)
        g = CM.ContextGovernor(3000, summarizer=stub,
                               clock=lambda: 1750000000.0)
        out, _ = g.ensure_before_dispatch(msgs)
        request = stub.last_request
        prefix_blob = json.dumps(request[:-1], ensure_ascii=False,
                                 sort_keys=True)
        # no wall-clock time value from the injected fake clock leaked in
        self.assertNotIn("1750000000", prefix_blob)
        # no ISO-8601-like date pattern leaked into the prefix
        import re
        self.assertIsNone(re.search(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}",
                                    prefix_blob))
        # source-level guardrail: the mechanism builds the request tail from
        # exactly the module constants + the caller's messages; the only
        # literal format usage in the request builder is the instruction
        with open(CM.__file__, encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn("SUMMARY_INSTRUCTION", src)
        self.assertNotIn("time.strftime", src)
        self.assertNotIn("datetime.now", src)

    def test_summary_guard_still_compares_against_middle_only(self):
        # boundary 2 unchanged: the summary competes with the shadowed
        # middle, not with the (longer) full request
        big = MessageSummarizer(out="S" * 20000)     # bigger than the middle
        msgs = build_chat_session(n_rounds=6)
        with self.assertRaises(CM.ContextMechanismError) as ctx:
            CM.compact_l2(msgs, 3000, big,
                          CM.session_prefix_for(msgs))
        self.assertEqual(ctx.exception.code, "SUMMARY_NOT_SMALLER")
        self.assertEqual(big.calls, 1)

    def test_l2_still_exactly_one_llm_call(self):
        # retry count unchanged: exactly 1 summarizer invocation per L2
        stub = MessageSummarizer()
        msgs = build_chat_session(n_rounds=20)
        g = CM.ContextGovernor(3000, summarizer=stub)
        out, changed = g.ensure_before_dispatch(msgs)
        self.assertEqual(stub.calls, 1)
        self.assertEqual(g.llm_calls, 1)
        self.assertTrue(changed)

    def test_original_session_not_mutated_by_request_building(self):
        # the store stays the single source of truth: building the request
        # never writes into the caller's message dicts
        stub = MessageSummarizer()
        msgs = build_chat_session(n_rounds=20)
        orig = copy.deepcopy(msgs)
        g = CM.ContextGovernor(3000, summarizer=stub)
        g.ensure_before_dispatch(msgs)
        self.assertEqual(msgs, orig)


if __name__ == "__main__":
    unittest.main()
