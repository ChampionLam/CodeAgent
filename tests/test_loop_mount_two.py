"""Mount point 2 (request-error) wiring tests for agent_loop (contract 7.4).

These assert the *wiring* of ContextGovernor.on_request_error into
_run_inner's stream-error path, not the mechanism itself (that lives in
test_context_mechanism.py). All dependencies are fakes: zero network, zero
shell, zero database.

The fakes follow tests/test_agent_loop.py's harness: a scripted llm_stream
plus a make_deps() builder. The scripted stream is extended with per-call
event lists so a first call can end in "error" and a second in "done".
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "python"))

import context_mechanism as CM  # noqa: E402
import agent_loop as AL  # noqa: E402
from agent_loop import LoopContext, LoopDeps, run  # noqa: E402


class FakeConfig:
    model = "fake-model"
    max_tokens = 128


#: 旧测试用的是分级名，这里做一层映射，免得每个用例都改一遍
_TIER_ALIASES = {"L0": "", "L1": "dangerous", "L2": "dangerous", "L3": "hardline"}


class FakeDecision:
    """guard.Verdict 的假实现。2026-09-25 起口径是 tier（hardline / dangerous / ""），
    不再是 L0-L3 分级；can_always_allow 恒 false（「总是允许」已取消）。"""

    def __init__(self, tier="", requires_approval=False, can_always_allow=False, reason="test"):
        self.tier = _TIER_ALIASES.get(tier, tier)
        self.pattern = ""
        self.requires_approval = requires_approval
        self.can_always_allow = False
        self.reason = reason


def make_llm(scripts):
    """scripts: a list of per-call event lists, consumed in order.

    Records every call's messages snapshot and kwargs so tests can assert
    on what the loop actually dispatched (messages and max_tokens).
    """
    calls = []

    def stream(cfg, messages, **kw):
        calls.append({"messages": [dict(m) for m in messages], "kwargs": kw})
        idx = min(len(calls) - 1, len(scripts) - 1)
        return iter([dict(e) for e in scripts[idx]])

    stream.calls = calls
    return stream


def text_script(text="ok", finish="stop"):
    return [{"type": "delta", "text": text},
            {"type": "done", "finishReason": finish,
             "usage": {"total_tokens": 5}, "toolCalls": []}]


def error_script(code, message, http_status=None):
    ev = {"type": "error", "code": code, "message": message}
    if http_status is not None:
        ev["httpStatus"] = http_status
    return [ev]


def make_deps(llm, governor=None, cancelled=lambda: False):
    deps = LoopDeps(
        llm_stream=llm,
        request_approval=lambda payload: "deny",
        load_model_config=lambda: FakeConfig(),
        exec_tool=lambda name, args, ws: None,
        judge=lambda name, args: FakeDecision(),
        audit=None,
        is_cancelled=cancelled,
        tool_schemas=lambda: [],
        model_declares_image=lambda m: True,
        splitter_factory=None,
        context_governor=governor,
    )
    return deps


def kinds(events):
    return [e["type"] for e in events]


def build_big_session(n_rounds=20, turn_chars=2000):
    msgs = [{"role": "system", "content": "SYS-PROMPT-BYTES"},
            {"role": "user", "content": "Q" * turn_chars}]
    for i in range(n_rounds):
        msgs.append({"role": "assistant", "content": "A" * turn_chars})
        msgs.append({"role": "user", "content": "U" * (turn_chars // 4)})
    return msgs


class StubGovernor:
    """Deterministic fake governor: answers on_request_error from a script.

    script: list of decisions consumed one per on_request_error call.
    events: list of context.compacted-style raw events handed out by
    drain_events() (drained once).
    """

    def __init__(self, decisions, events=None):
        self.decisions = list(decisions)
        self.errors_seen = []
        self.messages_seen = []
        self._events = list(events or [])

    def on_request_error(self, messages, error):
        self.errors_seen.append(dict(error))
        self.messages_seen.append([dict(m) for m in messages])
        idx = min(len(self.errors_seen) - 1, len(self.decisions) - 1)
        return dict(self.decisions[idx])

    def drain_events(self):
        out, self._events = self._events, []
        return out


class ExplodingGovernor:
    def on_request_error(self, messages, error):
        raise RuntimeError("governor exploded")

    def drain_events(self):
        return []


# ---------------------------------------------------------------------------
# a) / b): governor absent or broken -> exactly the old behaviour
# ---------------------------------------------------------------------------


class NoGovernorTest(unittest.TestCase):
    def test_none_governor_yields_error_and_single_call(self):
        llm = make_llm([error_script("AUTH", "bad key")])
        ctx = LoopContext(messages=[{"role": "user", "content": "x"}])
        events = list(run(ctx, deps=make_deps(llm, governor=None)))
        self.assertEqual(kinds(events), ["chat.error"])
        self.assertEqual(events[0]["code"], "AUTH")
        self.assertEqual(events[0]["message"], "bad key")
        self.assertEqual(len(llm.calls), 1)          # no second model call

    def test_broken_governor_still_yields_error_and_does_not_raise(self):
        llm = make_llm([error_script("AUTH", "bad key")])
        ctx = LoopContext(messages=[{"role": "user", "content": "x"}])
        events = list(run(ctx, deps=make_deps(llm, governor=ExplodingGovernor())))
        self.assertEqual(kinds(events), ["chat.error"])
        self.assertEqual(events[0]["code"], "AUTH")
        self.assertEqual(events[0]["message"], "bad key")
        self.assertEqual(len(llm.calls), 1)


# ---------------------------------------------------------------------------
# c): propagate -> one chat.error, one model call
# ---------------------------------------------------------------------------


class PropagateTest(unittest.TestCase):
    def test_propagate_single_error_single_call(self):
        gov = StubGovernor([{"action": "propagate"}])
        llm = make_llm([error_script("AUTH", "invalid api key", 401)])
        ctx = LoopContext(messages=[{"role": "user", "content": "x"}])
        events = list(run(ctx, deps=make_deps(llm, governor=gov)))
        self.assertEqual(kinds(events), ["chat.error"])
        self.assertEqual(events[0]["code"], "AUTH")
        self.assertEqual(events[0]["message"], "invalid api key")
        self.assertEqual(len(llm.calls), 1)
        # the governor saw the normalized error shape
        self.assertEqual(gov.errors_seen[0]["code"], "AUTH")
        self.assertEqual(gov.errors_seen[0]["httpStatus"], 401)

    def test_abort_action_also_propagates(self):
        gov = StubGovernor([{"action": "abort", "reason": "NETWORK"}])
        llm = make_llm([error_script("ERR", "connection reset by peer")])
        events = list(run(LoopContext(messages=[{"role": "user", "content": "x"}]),
                          deps=make_deps(llm, governor=gov)))
        self.assertEqual(kinds(events), ["chat.error"])
        self.assertEqual(events[0]["code"], "ERR")
        self.assertEqual(len(llm.calls), 1)

    def test_unknown_action_treated_as_propagate(self):
        gov = StubGovernor([{"action": "something_new"}])
        llm = make_llm([error_script("X", "weird")])
        events = list(run(LoopContext(messages=[{"role": "user", "content": "x"}]),
                          deps=make_deps(llm, governor=gov)))
        self.assertEqual(kinds(events), ["chat.error"])
        self.assertEqual(events[0]["code"], "X")
        self.assertEqual(len(llm.calls), 1)


# ---------------------------------------------------------------------------
# d) / e): lower_max_tokens
# ---------------------------------------------------------------------------


class LowerMaxTokensTest(unittest.TestCase):
    def test_second_dispatch_uses_lowered_value_and_ctx_untouched(self):
        gov = StubGovernor([{"action": "lower_max_tokens", "maxTokens": 2000}])
        llm = make_llm([error_script("400", "max_tokens too large: 9000 > 8192", 400),
                        text_script("ok now")])
        ctx = LoopContext(messages=[{"role": "user", "content": "x"}], max_tokens=8192)
        events = list(run(ctx, deps=make_deps(llm, governor=gov)))
        self.assertEqual(events[-1]["type"], "chat.done")     # done, not error
        self.assertEqual(len(llm.calls), 2)
        self.assertEqual(llm.calls[0]["kwargs"]["max_tokens"], 8192)
        self.assertEqual(llm.calls[1]["kwargs"]["max_tokens"], 2000)
        self.assertEqual(ctx.max_tokens, 8192)                # session value intact
        # the failed round left no assistant message in the store
        roles = [m["role"] for m in ctx.messages]
        self.assertEqual(roles, ["user", "assistant"])
        self.assertEqual(ctx.messages[-1]["content"], "ok now")

    def test_second_lower_request_propagates_without_resend(self):
        decisions = [{"action": "lower_max_tokens", "maxTokens": 2000},
                     {"action": "lower_max_tokens", "maxTokens": 1000}]
        gov = StubGovernor(decisions)
        llm = make_llm([error_script("400", "max_tokens too large: 9000 > 8192", 400),
                        error_script("400", "max_tokens too large: 2000 > 1500", 400)])
        ctx = LoopContext(messages=[{"role": "user", "content": "x"}], max_tokens=8192)
        events = list(run(ctx, deps=make_deps(llm, governor=gov)))
        # exactly one resend happened; the second lower request became chat.error
        self.assertEqual(len(llm.calls), 2)
        self.assertEqual(events[-1]["type"], "chat.error")
        self.assertEqual(events[-1]["code"], "400")
        err_events = [e for e in events if e["type"] == "chat.error"]
        self.assertEqual(len(err_events), 1)
        # nothing was appended to the session on the failed rounds
        self.assertEqual([m["role"] for m in ctx.messages], ["user"])


# ---------------------------------------------------------------------------
# f) / g): compact_retry
# ---------------------------------------------------------------------------


class CompactRetryTest(unittest.TestCase):
    def _big_ctx(self):
        return LoopContext(messages=build_big_session(), max_tokens=8192)

    def test_second_dispatch_uses_governor_messages_and_event_first(self):
        compacted = [{"role": "user", "content": "compacted view"}]
        raw_event = {"event": "context.compacted",
                     "data": {"level": "L2", "trigger": "context-overflow",
                              "estimatedTokens": 90000, "tokensAfter": 4000,
                              "llmCalls": 1}}
        gov = StubGovernor([{"action": "compact_retry", "messages": compacted}],
                           events=[raw_event])
        llm = make_llm([error_script("CONTEXT_WINDOW_EXCEEDED", "context window exceeded"),
                        text_script("recovered")])
        ctx = self._big_ctx()
        events = list(run(ctx, deps=make_deps(llm, governor=gov)))
        # contract 7.5: context.compacted is yielded BEFORE the retry outcome
        self.assertEqual(kinds(events), ["context.compacted", "chat.delta", "chat.done"])
        self.assertEqual(events[0]["type"], "context.compacted")
        self.assertEqual(events[0]["trigger"], "context-overflow")
        self.assertEqual(events[-1]["type"], "chat.done")
        # the second dispatch really used the governor-provided messages
        self.assertEqual(len(llm.calls), 2)
        self.assertEqual(llm.calls[1]["messages"], compacted)
        self.assertLess(len(llm.calls[1]["messages"]), len(llm.calls[0]["messages"]))
        # the session store was NOT replaced by the compacted view; only the
        # successful round appended its assistant reply
        self.assertEqual(len(ctx.messages), len(build_big_session()) + 1)
        self.assertEqual(ctx.messages[-1]["content"], "recovered")

    def test_real_governor_compact_retry_changes_messages(self):
        # a real governor with a stub summarizer: preset to COOLING so that
        # mount 1 (pre-step) sends the session verbatim (per its docstring)
        # and mount 2 does the actual overflow recovery. Then the projected
        # messages the loop re-dispatches must differ from the first attempt.
        gov = CM.ContextGovernor(3000, summarizer=lambda text: "stub summary")
        gov.state = CM.COOLING
        llm = make_llm([error_script("CONTEXT_WINDOW_EXCEEDED", "context window exceeded"),
                        text_script("recovered")])
        ctx = LoopContext(messages=build_big_session(), max_tokens=8192)
        events = list(run(ctx, deps=make_deps(llm, governor=gov)))
        self.assertEqual(len(llm.calls), 2)
        first, second = llm.calls[0]["messages"], llm.calls[1]["messages"]
        self.assertLess(len(second), len(first))
        self.assertTrue(any("summary" in str(m.get("content", "")) for m in second))
        self.assertEqual(events[-1]["type"], "chat.done")
        # the compacted event precedes the retry outcome
        self.assertEqual(events[0]["type"], "context.compacted")
        # session store untouched (still the full history, plus the final
        # assistant reply from the successful round)
        self.assertEqual(len(ctx.messages), len(build_big_session()) + 1)
        self.assertEqual(ctx.messages[-1]["role"], "assistant")
        self.assertEqual(ctx.messages[-1]["content"], "recovered")

    def test_third_compact_retry_becomes_error(self):
        decisions = [{"action": "compact_retry", "messages": [{"role": "user", "content": "v1"}]},
                     {"action": "compact_retry", "messages": [{"role": "user", "content": "v2"}]},
                     {"action": "compact_retry", "messages": [{"role": "user", "content": "v3"}]}]
        gov = StubGovernor(decisions)
        llm = make_llm([error_script("CONTEXT_WINDOW_EXCEEDED", "overflow"),
                        error_script("CONTEXT_WINDOW_EXCEEDED", "overflow"),
                        error_script("CONTEXT_WINDOW_EXCEEDED", "overflow")])
        ctx = self._big_ctx()
        events = list(run(ctx, deps=make_deps(llm, governor=gov)))
        # budget is OVERFLOW_RETRY (2): two resends, then the third turns into
        # a single chat.error with the original code
        self.assertEqual(len(llm.calls), 3)
        err_events = [e for e in events if e["type"] == "chat.error"]
        self.assertEqual(len(err_events), 1)
        self.assertEqual(events[-1]["type"], "chat.error")
        self.assertEqual(events[-1]["code"], "CONTEXT_WINDOW_EXCEEDED")
        self.assertEqual(events[-1]["message"], "overflow")

    def test_retry_budget_matches_overflow_retry_constant(self):
        # the wiring must use context_mechanism.OVERFLOW_RETRY, not a literal
        decisions = ([{"action": "compact_retry",
                       "messages": [{"role": "user", "content": "v"}]}] * 10)
        gov = StubGovernor(decisions)
        scripts = [error_script("CONTEXT_WINDOW_EXCEEDED", "overflow")] * 10
        llm = make_llm(scripts)
        events = list(run(self._big_ctx(), deps=make_deps(llm, governor=gov)))
        self.assertEqual(len(llm.calls), CM.OVERFLOW_RETRY + 1)  # initial + budget
        self.assertEqual(events[-1]["type"], "chat.error")


# ---------------------------------------------------------------------------
# h): tripped
# ---------------------------------------------------------------------------


class TrippedTest(unittest.TestCase):
    def test_tripped_single_error_with_warning_and_exits(self):
        gov = StubGovernor([{
            "action": "tripped",
            "warning": "compaction cannot bring the session back inside the window",
            "exits": ["start_new_session", "manual_retry_force_compact"],
        }])
        llm = make_llm([error_script("CONTEXT_WINDOW_EXCEEDED", "overflow")])
        events = list(run(LoopContext(messages=[{"role": "user", "content": "x"}]),
                          deps=make_deps(llm, governor=gov)))
        self.assertEqual(kinds(events), ["chat.error"])
        err = events[0]
        self.assertEqual(err["code"], "CONTEXT_TRIPPED")
        self.assertIn("compaction cannot bring the session back inside the window",
                      err["message"])
        self.assertIn("start_new_session", err["message"])
        self.assertIn("manual_retry_force_compact", err["message"])
        self.assertEqual(len(llm.calls), 1)


# ---------------------------------------------------------------------------
# i): static guard - never write ctx.max_tokens
# ---------------------------------------------------------------------------


class StaticGuardTest(unittest.TestCase):
    def test_agent_loop_never_assigns_ctx_max_tokens(self):
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "python", "agent_loop.py")
        with open(path, encoding="utf-8") as f:
            src = f.read()
        self.assertNotIn("ctx.max_tokens =", src,
                         "agent_loop must never assign ctx.max_tokens (the session "
                         "window/output cap is read-only from the loop's perspective)")
        self.assertNotIn("ctx.max_tokens=", src)


if __name__ == "__main__":
    unittest.main()
