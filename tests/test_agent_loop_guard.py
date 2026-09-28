"""agent_loop 护栏单测：重复调用 + 连续失败的预算，且拒绝必须发生在审批之前。

2026-09-24 现场：一条 "现在几点,深圳天气怎样" 里 8 次 run_shell、8 次审批弹窗，
因为每次失败后模型都换个写法再来一次，而每个"新写法"都是一次全新的审批。
"""
from __future__ import annotations

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "python"))

import agent_loop as AL  # noqa: E402
from agent_loop import LoopContext, LoopDeps, run  # noqa: E402
from reasoning_split import ReasoningSplitter  # noqa: E402


class FakeConfig:
    model = "fake-model"
    max_tokens = 128


class FakeDecision:
    level = "L2"
    requires_approval = True
    can_always_allow = False
    reason = "test"


class FakeToolResult:
    def __init__(self, ok=True, content="out", error_code=None, error_message=None):
        self.ok = ok
        self.content = content
        self.error_code = error_code
        self.error_message = error_message


def scripts_from(arguments, tool="run_shell"):
    """每轮一个工具调用，最后补一轮纯文本收尾。"""
    scripts = []
    for i, args in enumerate(arguments):
        scripts.append([
            {"type": "delta", "text": ""},
            {"type": "done", "finishReason": "tool_calls", "usage": {"total_tokens": 1},
             "toolCalls": [{"id": "c%d" % i, "type": "function", "name": tool,
                            "arguments": json.dumps(args)}]},
        ])
    scripts.append([{"type": "delta", "text": "结束"},
                    {"type": "done", "finishReason": "stop", "usage": {}, "toolCalls": []}])
    return scripts


def scripted_llm(scripts):
    calls = []

    def stream(cfg, messages, **kw):
        calls.append(1)
        return iter(scripts[min(len(calls) - 1, len(scripts) - 1)])

    stream.calls = calls
    return stream


def make_deps(llm, result, lessons=None):
    approvals = []
    execs = []

    def exec_tool(name, args, workspace_root):
        execs.append(args)
        return result() if callable(result) else result

    def request_approval(payload):
        approvals.append(payload)
        return "allow_once"

    deps = LoopDeps(
        llm_stream=llm,
        request_approval=request_approval,
        load_model_config=lambda: FakeConfig(),
        exec_tool=exec_tool,
        judge=lambda name, args: FakeDecision(),
        audit=None,
        is_cancelled=lambda: False,
        tool_schemas=lambda: [],
        model_declares_image=lambda m: True,
        splitter_factory=ReasoningSplitter,
        lessons=lessons,
    )
    deps._approvals = approvals
    deps._execs = execs
    return deps


def error_codes(events):
    return [e.get("code") for e in events if e["type"] == AL.EVENT_ERROR]


class FakeLessons:
    """经验库假件：只记调用，不落盘。"""

    def __init__(self):
        self.recorded = []
        self.successes = []

    def record(self, tool, args, code, message):
        self.recorded.append((tool, code, message))

    def note_success(self, tool, args):
        self.successes.append(tool)


def fail_refusals(events):
    """被工具失败守卫拒掉的调用。"""
    return [e for e in events
            if e["type"] == AL.EVENT_TOOL_RESULT and e.get("errorCode") == AL.TOOL_FAILURE_LIMIT]


def dup_refusals(events):
    return [e for e in events
            if e["type"] == AL.EVENT_TOOL_RESULT and e.get("errorCode") == AL.DUPLICATE_TOOL]


class DuplicateBeforeApprovalTest(unittest.TestCase):
    def test_identical_call_is_refused_without_a_second_prompt(self):
        """重复调用只拒这一次，不弹第二次审批 —— 也不再当场掐死整轮。"""
        llm = scripted_llm(scripts_from([{"command": "echo a"}, {"command": "echo a"}]))
        deps = make_deps(llm, FakeToolResult())
        ctx = LoopContext(messages=[{"role": "user", "content": "hi"}], max_loop=5)
        events = list(run(ctx, deps=deps))
        self.assertEqual(len(deps._approvals), 1)
        self.assertEqual(len(deps._execs), 1)
        self.assertEqual(len(dup_refusals(events)), 1)
        self.assertEqual(error_codes(events), [], "单次重复不该终止本轮")

    def test_duplicate_refusal_carries_actionable_hint(self):
        """被拒时要告诉模型怎么改（2026-09-25：读扫描件续页被原样重发卡死）。"""
        llm = scripted_llm(scripts_from([{"command": "echo a"}, {"command": "echo a"}]))
        deps = make_deps(llm, FakeToolResult())
        ctx = LoopContext(messages=[{"role": "user", "content": "hi"}], max_loop=5)
        events = list(run(ctx, deps=deps))
        note = dup_refusals(events)[0]["content"]
        self.assertIn("换参数或换工具", note)

    def test_refused_twice_then_model_continues_with_new_args(self):
        """拒两次之后模型换参数 → 正常执行，本轮不报错。"""
        llm = scripted_llm(scripts_from([{"command": "echo a"}, {"command": "echo a"},
                                         {"command": "echo a"}, {"command": "echo b"}]))
        deps = make_deps(llm, FakeToolResult())
        ctx = LoopContext(messages=[{"role": "user", "content": "hi"}], max_loop=8)
        events = list(run(ctx, deps=deps))
        self.assertEqual(len(deps._execs), 2, "两次成功执行：echo a 一次 + echo b 一次")
        self.assertEqual(error_codes(events), [])

    def test_keeps_repeating_then_hard_stop(self):
        """连犯超过预算才终止（避免模型拿同一调用无限烧循环）。"""
        calls = [{"command": "echo a"} for _ in range(8)]
        llm = scripted_llm(scripts_from(calls))
        deps = make_deps(llm, FakeToolResult())
        ctx = LoopContext(messages=[{"role": "user", "content": "hi"}], max_loop=10)
        events = list(run(ctx, deps=deps))
        self.assertEqual(len(deps._approvals), 1)
        self.assertEqual(len(deps._execs), 1)
        self.assertEqual(error_codes(events)[-1], AL.DUPLICATE_TOOL)
        self.assertEqual(len(dup_refusals(events)), AL.MAX_DUPLICATE_REFUSALS + 1)

    def test_read_file_hint_points_at_pages(self):
        """read_file 的重复提示要说清「只改 pages 参数」。"""
        note = AL._duplicate_hint("read_file", {"path": "a.pdf"})
        self.assertIn("pages", note)
        self.assertIn("9-34", note)
        other = AL._duplicate_hint("run_shell", {"command": "ls"})
        self.assertNotIn("pages", other)


class FailureBudgetTest(unittest.TestCase):
    def test_tool_cools_down_and_stops_prompting(self):
        """工具级失败到预算就冷却：不再执行、不再弹审批（2026-09-25 新语义）。"""
        cmds = [{"command": "try %d" % i} for i in range(10)]
        llm = scripted_llm(scripts_from(cmds))
        deps = make_deps(llm, FakeToolResult(ok=False, error_code="NONZERO_EXIT", error_message="boom"))
        ctx = LoopContext(messages=[{"role": "user", "content": "hi"}], max_loop=12)
        events = list(run(ctx, deps=deps))
        self.assertEqual(len(deps._approvals), AL.TOOL_FAIL_TOTAL_BUDGET)
        self.assertEqual(len(deps._execs), AL.TOOL_FAIL_TOTAL_BUDGET)
        self.assertIn(AL.TOOL_FAILURE_LIMIT, error_codes(events))

    def test_success_resets_the_streak(self):
        cmds = [{"command": "c%d" % i} for i in range(5)]
        llm = scripted_llm(scripts_from(cmds))
        seq = [False, True, False, True, False]
        state = {"i": 0}

        def result():
            ok = seq[state["i"]]
            state["i"] += 1
            return FakeToolResult(ok=ok, error_code=None if ok else "NONZERO_EXIT")

        deps = make_deps(llm, result)
        ctx = LoopContext(messages=[{"role": "user", "content": "hi"}], max_loop=10)
        events = list(run(ctx, deps=deps))
        self.assertNotIn(AL.TOOL_FAILURE_LIMIT, error_codes(events))
        self.assertEqual(len(deps._execs), 5)


class ToolFailureEscalationTest(unittest.TestCase):
    """2026-09-25 真机：模型连续抓 3 个**不同**来源失败，被当成「重复重试」掐死整轮。"""

    def _ctx(self, max_loop=8):
        return LoopContext(messages=[{"role": "user", "content": "hi"}], max_loop=max_loop)

    def _failing(self, message="403 Forbidden"):
        return FakeToolResult(ok=False, error_code="HTTP_ERROR", error_message=message)

    def test_different_targets_failing_is_exploration_not_a_stop(self):
        urls = [{"url": "https://a.com/1"}, {"url": "https://b.com/1"}, {"url": "https://c.com/1"}]
        llm = scripted_llm(scripts_from(urls, tool="web_fetch"))
        deps = make_deps(llm, self._failing())
        events = list(run(self._ctx(), deps=deps))
        self.assertEqual(len(deps._execs), 3, "换来源要先让它真跑")
        self.assertEqual(fail_refusals(events), [])
        self.assertEqual(error_codes(events), [], "换来源连败不许终止本轮")

    def test_same_call_refused_after_two_failures_with_real_error(self):
        arg = {"url": "https://a.com/1"}
        llm = scripted_llm(scripts_from([arg, arg, arg], tool="web_fetch"))
        deps = make_deps(llm, self._failing())
        events = list(run(self._ctx(), deps=deps))
        self.assertEqual(len(deps._execs), 2, "同样的调用跑两次就够，第三次拒")
        ref = fail_refusals(events)
        self.assertEqual(len(ref), 1)
        self.assertIn("403 Forbidden", ref[0]["content"], "拒绝时必须带真实错误原文")
        self.assertIn("换来源", ref[0]["content"], "拒绝时必须给可操作替代方案")
        self.assertEqual(error_codes(events), [], "单次拒绝不终止本轮")

    def test_tool_cools_down_after_total_failures(self):
        urls = [{"url": "https://s%d.com/x" % i} for i in range(7)]
        llm = scripted_llm(scripts_from(urls, tool="web_fetch"))
        deps = make_deps(llm, self._failing())
        events = list(run(self._ctx(max_loop=10), deps=deps))
        self.assertEqual(len(deps._execs), 6, "工具级预算 6 次")
        ref = fail_refusals(events)
        self.assertTrue(ref, "第 7 次换参数也该被冷却拒掉")
        self.assertIn("冷却", ref[0]["content"])

    def test_refusing_forever_still_ends_the_turn(self):
        arg = {"url": "https://a.com/1"}
        llm = scripted_llm(scripts_from([arg] * 8, tool="web_fetch"))
        deps = make_deps(llm, self._failing())
        events = list(run(self._ctx(max_loop=12), deps=deps))
        self.assertEqual(error_codes(events), [AL.TOOL_FAILURE_LIMIT], "只终止一次，不刷屏")

    def test_lessons_record_only_persistent_failures(self):
        store = FakeLessons()
        urls = [{"url": "https://a.com/1"}, {"url": "https://b.com/1"}]
        llm = scripted_llm(scripts_from(urls, tool="web_fetch"))
        deps = make_deps(llm, self._failing(), lessons=store)
        list(run(self._ctx(), deps=deps))
        self.assertEqual(store.recorded, [], "偶发失败不记经验")

    def test_lessons_record_same_call_twice_and_degrade_on_success(self):
        store = FakeLessons()
        arg = {"url": "https://a.com/1"}
        llm = scripted_llm(scripts_from([arg, arg], tool="web_fetch"))
        deps = make_deps(llm, self._failing(), lessons=store)
        list(run(self._ctx(), deps=deps))
        self.assertEqual(len(store.recorded), 1, "同参数失败 2 次才记一条经验")
        self.assertEqual(store.recorded[0][1], "HTTP_ERROR")

        store2 = FakeLessons()
        ok_deps = make_deps(scripted_llm(scripts_from([arg], tool="web_fetch")),
                            FakeToolResult(ok=True), lessons=store2)
        list(run(self._ctx(), deps=ok_deps))
        self.assertEqual(store2.successes, ["web_fetch"], "成功要降权（自我纠正）")

    def test_broken_lessons_store_does_not_break_the_loop(self):
        class Boom:
            def record(self, *a, **k):
                raise RuntimeError("disk full")

            def note_success(self, *a, **k):
                raise RuntimeError("disk full")

        arg = {"url": "https://a.com/1"}
        llm = scripted_llm(scripts_from([arg, arg, arg], tool="web_fetch"))
        deps = make_deps(llm, self._failing(), lessons=Boom())
        events = list(run(self._ctx(), deps=deps))
        self.assertEqual(events[-1]["type"], AL.EVENT_DONE, "经验库炸了也不许影响本轮")


if __name__ == "__main__":
    unittest.main()