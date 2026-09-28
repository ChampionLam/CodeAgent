"""上下文机制 v1 单测：确定性桩（假度量不引入、假摘要器、假时钟），零网络。

覆盖验收（方案「验收标准」节的 v1 部分 + 契约 §7.3 五条硬边界）：
  * 免费降级零 LLM 调用（计数器读数为 0）
  * 压缩前后系统提示字节级不变
  * 超额恢复只在状态真变化时重试
  * 反抖动不出第三次压缩
  * tool 配对 / 角色交替边界不被破坏
  * 加载期校验拒绝不自洽窗口
  * L0/L1/L2 顺序、单条 30% / 聚合 50% 落盘
  * 挂载点二分流：输出上限只降 max_tokens、鉴权类中止原样保留
"""
from __future__ import annotations

import copy
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "python"))

import context_mechanism as CM  # noqa: E402


def build_tool_session(n_tools: int = 8, content: str = "A" * 3000) -> list[dict]:
    msgs: list[dict] = [{"role": "system", "content": "SYS-PROMPT-BYTES"}]
    msgs.append({"role": "user", "content": "hi"})
    for i in range(n_tools):
        msgs.append({
            "role": "assistant", "content": "",
            "tool_calls": [{
                "id": "c%d" % i, "type": "function",
                "function": {"name": "read_file",
                             "arguments": '{"path": "f%d.txt"}' % i}}]})
        msgs.append({"role": "tool", "tool_call_id": "c%d" % i, "content": content})
    msgs.append({"role": "user", "content": "go on"})
    return msgs


def build_chat_session(n_rounds: int = 20, turn_chars: int = 2000) -> list[dict]:
    msgs: list[dict] = [{"role": "system", "content": "SYS-PROMPT-BYTES"}]
    msgs.append({"role": "user", "content": "Q" * turn_chars})
    for i in range(n_rounds):
        msgs.append({"role": "assistant", "content": "A" * turn_chars})
        msgs.append({"role": "user", "content": "U" * (turn_chars // 4)})
    return msgs


class StubSummarizer:
    """确定性假摘要器：返回固定短文本；可数被调用次数。"""

    def __init__(self, out: str = "stub summary of the middle"):
        self.out = out
        self.calls = 0
        self.seen: list[str] = []

    def __call__(self, text: str) -> str:
        self.calls += 1
        self.seen.append(text)
        return self.out


class FakeClock:
    """假时钟：手动推进，不碰 time.time。"""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, s: float) -> None:
        self.now += s


# ---------------------------------------------------------------------------
# 1. 预算与加载期校验
# ---------------------------------------------------------------------------


class BudgetTest(unittest.TestCase):
    def test_trigger_point_formula(self):
        # 窗口 128000：min(102400, 128000-16384=111616) = 102400
        self.assertEqual(CM.trigger_point(128000), 102400)
        # 小窗口预留被 25% 钳制：窗口 10000 -> 预留 2500，触发点 min(8000, 7500)=7500
        self.assertEqual(CM.trigger_point(10000), 7500)
        self.assertEqual(CM.reserve_tokens(128000), 16384)
        self.assertEqual(CM.reserve_tokens(10000), 2500)
        self.assertEqual(CM.tail_tokens(10000), 1600)

    def test_load_validation_rejects_bad_window(self):
        # 保留尾部必须 < 触发点；窗口太小/非法直接 MODEL_INVALID
        with self.assertRaises(CM.ContextMechanismError) as ctx:
            CM.ContextGovernor(window=1)
        self.assertEqual(ctx.exception.code, "MODEL_INVALID")
        with self.assertRaises(CM.ContextMechanismError):
            CM.ContextGovernor(window=0)
        with self.assertRaises(CM.ContextMechanismError):
            CM.ContextGovernor(window=-5)

    def test_window_from_model_entry_defensive(self):
        self.assertEqual(CM.window_from_model_entry({}), 128000)
        self.assertEqual(CM.window_from_model_entry({"contextWindow": 200000}), 200000)
        self.assertEqual(CM.window_from_model_entry({"contextWindow": "junk"}), 128000)
        self.assertEqual(CM.window_from_model_entry(None), 128000)
        ns = type("NS", (), {"contextWindow": 64000})()
        self.assertEqual(CM.window_from_model_entry(ns), 64000)


# ---------------------------------------------------------------------------
# 2. 估算口径
# ---------------------------------------------------------------------------


class EstimationTest(unittest.TestCase):
    def test_ascii_and_cjk(self):
        self.assertEqual(CM.estimate_text_tokens("a" * 40), 10)
        self.assertEqual(CM.estimate_text_tokens("中" * 10), 10)
        # 混合：10 CJK + 8 ASCII = 10 + 2
        self.assertEqual(CM.estimate_text_tokens("中" * 10 + "a" * 8), 12)

    def test_message_overhead_and_tool_calls(self):
        base = CM.estimate_message_tokens({"role": "user", "content": ""})
        self.assertGreaterEqual(base, 4)
        with_tc = CM.estimate_message_tokens(
            {"role": "assistant", "content": "",
             "tool_calls": [{"id": "x", "function": {"name": "f",
                                                     "arguments": "{}"}}]})
        self.assertGreater(with_tc, base)


# ---------------------------------------------------------------------------
# 3. L0：旧工具结果换一行摘要 + 去重（0 次 LLM）
# ---------------------------------------------------------------------------


class L0Test(unittest.TestCase):
    def test_old_tool_results_replaced_recent_5_kept(self):
        msgs = build_tool_session(n_tools=8)
        orig = copy.deepcopy(msgs)
        out = CM.l0_trim(msgs)
        # 原库不动（视图投影）
        self.assertEqual(msgs, orig)
        tool_msgs = [m for m in out if m["role"] == "tool"]
        # 最近 5 条原文保留
        self.assertEqual(tool_msgs[-5:], [m for m in msgs if m["role"] == "tool"][-5:])
        # 旧的三条被替换（第一条是结构化摘要行；内容相同 → 后续去重占位）
        replaced = tool_msgs[:-5]
        self.assertEqual(len(replaced), 3)
        self.assertIn("[工具 read_file(", replaced[0]["content"])
        self.assertIn("输出", replaced[0]["content"])
        for m in replaced:
            self.assertLess(len(m["content"]), 200)

    def test_dedup_identical_results(self):
        msgs = build_tool_session(n_tools=8)
        out = CM.l0_trim(msgs)
        tool_msgs = [m for m in out if m["role"] == "tool"]
        old = tool_msgs[:-5]
        # 相同内容的旧结果：第一条是摘要，后续是「重复」占位
        dupes = [m for m in old if "已去重" in m["content"]]
        self.assertEqual(len(dupes), len(old) - 1)

    def test_l0_zero_llm_by_counter(self):
        stub = StubSummarizer()
        g = CM.ContextGovernor(8000, summarizer=stub)
        msgs = build_tool_session(n_tools=8, content="A" * 3000)
        out, _ = g.ensure_before_dispatch(msgs)
        self.assertEqual(g.llm_calls, 0)
        self.assertEqual(stub.calls, 0)
        self.assertLess(CM.estimate_messages_tokens(out),
                        CM.estimate_messages_tokens(msgs))


# ---------------------------------------------------------------------------
# 4. L1：大输出落盘（单条 30% / 聚合 50%）
# ---------------------------------------------------------------------------


class L1Test(unittest.TestCase):
    def test_single_over_30pct_spilled(self):
        with tempfile.TemporaryDirectory() as d:
            store = CM.SpillStore(d)
            msgs = [{"role": "system", "content": "S"},
                    {"role": "user", "content": "hi"},
                    {"role": "assistant", "content": "", "tool_calls": [
                        {"id": "c1", "type": "function",
                         "function": {"name": "shell", "arguments": "{}"}}]},
                    {"role": "tool", "tool_call_id": "c1", "content": "B" * 20000}]
            out = CM.l1_spill(msgs, 10000, store)
            self.assertTrue(CM._is_spilled(out[3]))
            self.assertIn("path=", out[3]["content"])
            # 文件真的写盘了，内容与原文一致（原文永不丢）
            spilled_files = os.listdir(os.path.join(d, "spill"))
            self.assertEqual(len(spilled_files), 1)
            with open(os.path.join(d, "spill", spilled_files[0]), encoding="utf-8") as f:
                self.assertEqual(f.read(), "B" * 20000)
            # 上下文里只有定位符 + 头尾预览
            self.assertLess(len(out[3]["content"]), 800 + 400 + 200)

    def test_aggregate_over_50pct_spills_oldest_first(self):
        with tempfile.TemporaryDirectory() as d:
            store = CM.SpillStore(d)
            msgs = [{"role": "system", "content": "S"},
                    {"role": "user", "content": "hi"}]
            for i in range(8):
                msgs.append({"role": "assistant", "content": "", "tool_calls": [
                    {"id": "k%d" % i, "type": "function",
                     "function": {"name": "t", "arguments": "{}"}}]})
                msgs.append({"role": "tool", "tool_call_id": "k%d" % i,
                             "content": "C" * 4000})   # 每条约 1000 tok
            # 窗口 10000：聚合配额 5000。8 条 = 8000 tok > 5000
            out = CM.l1_spill(msgs, 10000, store)
            agg = sum(CM.estimate_text_tokens(CM._content_text(m))
                      for m in out if m["role"] == "tool")
            self.assertLessEqual(agg, 5000 + 1500)   # 允许头尾预览余量
            # 从最老开始落：第一个 tool 已落盘
            first_tool = next(m for m in out if m["role"] == "tool")
            self.assertTrue(CM._is_spilled(first_tool))

    def test_spill_failure_keeps_preview_not_full_text(self):
        store = CM.SpillStore(None)   # 没有目录 -> 落盘失败
        locator, path = store.spill("X" * 50000)
        self.assertEqual(path, "")
        msgs = [{"role": "system", "content": "S"},
                {"role": "user", "content": "hi"},
                {"role": "assistant", "content": "", "tool_calls": [
                    {"id": "c1", "type": "function",
                     "function": {"name": "sh", "arguments": "{}"}}]},
                {"role": "tool", "tool_call_id": "c1", "content": "X" * 50000}]
        out = CM.l1_spill(msgs, 10000, store)
        self.assertLess(len(out[3]["content"]), 50000)


# ---------------------------------------------------------------------------
# 5. L2：中段摘要（1 次 LLM）+ 摘要守卫
# ---------------------------------------------------------------------------


class L2Test(unittest.TestCase):
    def test_summary_guard_rejects_not_smaller(self):
        msgs = build_chat_session(n_rounds=6)
        big_stub = StubSummarizer(out="S" * 20000)   # 比中段还大
        with self.assertRaises(CM.ContextMechanismError) as ctx:
            CM.compact_l2(msgs, 3000, big_stub)
        self.assertEqual(ctx.exception.code, "SUMMARY_NOT_SMALLER")
        self.assertEqual(big_stub.calls, 1)

    def test_l2_exactly_one_llm_call(self):
        stub = StubSummarizer()
        msgs = build_chat_session(n_rounds=20)
        g = CM.ContextGovernor(3000, summarizer=stub)
        out, changed = g.ensure_before_dispatch(msgs)
        self.assertEqual(stub.calls, 1)
        self.assertEqual(g.llm_calls, 1)
        self.assertTrue(changed)
        self.assertLess(CM.estimate_messages_tokens(out),
                        CM.trigger_point(3000))
        evts = g.drain_events()
        self.assertEqual(len(evts), 1)
        self.assertEqual(evts[0]["event"], "context.compacted")
        self.assertEqual(evts[0]["data"]["level"], "L2")
        self.assertEqual(evts[0]["data"]["llmCalls"], 1)


# ---------------------------------------------------------------------------
# 6. 五条硬边界
# ---------------------------------------------------------------------------


class HardBoundaryTest(unittest.TestCase):
    def test_system_prompt_bytes_stable(self):
        # 验收 1：压缩前后 sha256(系统提示) 一致
        for builder in (build_tool_session, build_chat_session):
            msgs = builder()
            g = CM.ContextGovernor(3000, summarizer=StubSummarizer())
            out, _ = g.ensure_before_dispatch(msgs)
            self.assertEqual(CM.system_prompt_bytes(msgs),
                             CM.system_prompt_bytes(out))

    def test_system_prompt_bytes_identical_through_l0_l1_l2(self):
        msgs = build_tool_session(n_tools=8, content="A" * 3000)
        before = CM.system_prompt_bytes(msgs)
        step = CM.l0_trim(msgs)
        with tempfile.TemporaryDirectory() as d:
            step = CM.l1_spill(step, 8000, CM.SpillStore(d))
            step = CM.compact_l2(step, 3000, StubSummarizer())[0]
        self.assertEqual(before, CM.system_prompt_bytes(step))

    def test_orphan_tool_message_rejected(self):
        msgs = [{"role": "system", "content": "S"},
                {"role": "user", "content": "hi"},
                {"role": "tool", "tool_call_id": "ghost", "content": "x"}]
        with self.assertRaises(CM.ContextMechanismError):
            CM.check_pairs(msgs)

    def test_role_alternation_rejected(self):
        msgs = [{"role": "system", "content": "S"},
                {"role": "user", "content": "a"},
                {"role": "user", "content": "b"}]
        with self.assertRaises(CM.ContextMechanismError):
            CM.check_role_alternation(msgs)

    def test_pairs_survive_all_three_levels(self):
        msgs = build_tool_session(n_tools=8, content="A" * 3000)
        g = CM.ContextGovernor(3000, summarizer=StubSummarizer())
        out, _ = g.ensure_before_dispatch(msgs)
        CM.check_pairs(out)           # 不抛 = 配对完好
        CM.check_role_alternation(out)

    def test_projection_never_mutates_original(self):
        msgs = build_tool_session(n_tools=8)
        orig = copy.deepcopy(msgs)
        with tempfile.TemporaryDirectory() as d:
            g = CM.ContextGovernor(3000, summarizer=StubSummarizer(),
                                   spill_dir=d)
            g.ensure_before_dispatch(msgs)
        # 库是唯一事实源：原列表逐字节不变
        self.assertEqual(msgs, orig)


# ---------------------------------------------------------------------------
# 7. 状态机：反抖动 / 重试判据 / 冷却与熔断
# ---------------------------------------------------------------------------


class StateMachineTest(unittest.TestCase):
    def test_states_enumerated(self):
        self.assertEqual(set(CM.STATES), {"IDLE", "FREE_SCAN", "COMPACTING",
                                          "OVERFLOW_RECOVERING", "COOLING",
                                          "TRIPPED"})

    def test_anti_debounce_no_third_compaction(self):
        # 验收 8：构造「越压越满」（两次各省 < 10%）→ COOLING，无第三次
        class TinySavingSummarizer:
            def __init__(self):
                self.calls = 0

            def __call__(self, text):
                self.calls += 1
                # 摘要只比中段小一点点（省 < 10%）：返回中段去掉尾部一点
                return text[: int(len(text) * 0.93)] if text else "s"

        stub = TinySavingSummarizer()
        g = CM.ContextGovernor(3000, summarizer=stub)
        msgs = build_chat_session(n_rounds=20)
        first, _ = g.ensure_before_dispatch(msgs)
        first_events = len(g.drain_events())
        second, _ = g.ensure_before_dispatch(first)
        # 两次压缩后连续各省 < 10%：熔断
        self.assertEqual(g.state, "COOLING")
        self.assertLessEqual(stub.calls, 2)   # 没有第三次 L2
        third, _ = g.ensure_before_dispatch(second)
        self.assertEqual(stub.calls, 2)       # COOLING：不再压缩
        self.assertEqual(third, second)       # 原样送出

    def test_no_progress_no_overflow_retry(self):
        # 验收 4：stub「状态不前进」，断言不重试、进 TRIPPED
        g = CM.ContextGovernor(3000, summarizer=None)   # 压不出任何变化
        big = [{"role": "user", "content": "B" * 60000}]
        d = g.on_request_error(big, {"code": "CONTEXT_WINDOW_EXCEEDED",
                                     "message": "too long", "httpStatus": 400})
        self.assertEqual(d["action"], "tripped")
        self.assertEqual(g.state, "TRIPPED")
        self.assertEqual(g.overflow_attempts, 0)   # 无状态变化 -> 不计重试
        self.assertIn("start_new_session", d["exits"])
        self.assertIn("manual_retry_force_compact", d["exits"])
        self.assertIn("warning", d)

    def test_overflow_retry_only_with_state_change(self):
        stub = StubSummarizer()
        g = CM.ContextGovernor(3000, summarizer=stub)
        msgs = build_chat_session(n_rounds=20)
        d = g.on_request_error(msgs, {"code": "CONTEXT_WINDOW_EXCEEDED",
                                      "message": "context window exceeded",
                                      "httpStatus": 400})
        self.assertEqual(d["action"], "compact_retry")
        self.assertEqual(g.state, "OVERFLOW_RECOVERING")
        self.assertGreater(g.generation, 0)
        # 事件先于重试发出（契约 §7.5）
        evts = g.drain_events()
        self.assertEqual(evts[0]["event"], "context.compacted")
        self.assertEqual(evts[0]["data"]["trigger"], "context-overflow")


# ---------------------------------------------------------------------------
# 8. 挂载点二：识别 / 分流 / 失败分级
# ---------------------------------------------------------------------------


class Mount2Test(unittest.TestCase):
    def test_output_limit_lowers_max_tokens_never_window(self):
        g = CM.ContextGovernor(10000, summarizer=StubSummarizer())
        big = [{"role": "user", "content": "B" * 100000}]
        d = g.on_request_error(big, {"code": "400",
                                     "message": "max_tokens too large: 9000 > 8192",
                                     "httpStatus": 400})
        self.assertEqual(d["action"], "lower_max_tokens")
        self.assertLess(d["maxTokens"], 8192)
        # 绝不缩窗口
        self.assertEqual(g.window, 10000)
        self.assertNotIn("messages", d)   # 不做压缩

    def test_generic_400_plus_big_session_is_overflow(self):
        # 验收 5：通用 400 + 大会话 → 进 OVERFLOW_RECOVERING
        g = CM.ContextGovernor(3000, summarizer=StubSummarizer())
        big = build_chat_session(n_rounds=20)
        d = g.on_request_error(big, {"code": "BAD_REQUEST",
                                     "message": "something failed",
                                     "httpStatus": 400})
        self.assertEqual(d["action"], "compact_retry")
        self.assertEqual(g.state, "OVERFLOW_RECOVERING")

    def test_disconnect_plus_big_session_is_overflow_then_abort_class(self):
        # 验收 5：断连 + 大会话 → 识别为超额；失败分级里 NETWORK 是中止类
        g = CM.ContextGovernor(3000, summarizer=StubSummarizer())
        big = build_chat_session(n_rounds=20)
        d = g.on_request_error(big, {"code": "ERR",
                                     "message": "connection reset by peer"})
        self.assertEqual(d["action"], "abort")
        self.assertEqual(d["reason"], "NETWORK")
        self.assertEqual(g.state, "COOLING")
        # 原样保留消息：没给投影列表
        self.assertNotIn("messages", d)

    def test_auth_error_not_overflow_propagates(self):
        g = CM.ContextGovernor(3000, summarizer=StubSummarizer())
        d = g.on_request_error([{"role": "user", "content": "small"}],
                               {"code": "401", "message": "invalid api key"})
        self.assertEqual(d["action"], "propagate")

    def test_explicit_code_overflow(self):
        g = CM.ContextGovernor(3000, summarizer=StubSummarizer())
        msgs = build_chat_session(n_rounds=20)
        d = g.on_request_error(msgs, {"code": "CONTEXT_WINDOW_EXCEEDED",
                                      "message": ""})
        self.assertEqual(d["action"], "compact_retry")

    def test_small_session_400_not_overflow(self):
        g = CM.ContextGovernor(3000, summarizer=StubSummarizer())
        d = g.on_request_error([{"role": "user", "content": "x"}],
                               {"code": "BAD_REQUEST", "message": "bad json",
                                "httpStatus": 400})
        self.assertEqual(d["action"], "propagate")

    def test_failure_classes(self):
        self.assertEqual(CM.classify_failure({"code": "401",
                                              "message": "unauthorized"}), "AUTH")
        self.assertEqual(CM.classify_failure({"code": "429",
                                              "message": "rate limit"}), "QUOTA")
        self.assertEqual(CM.classify_failure({"code": "E",
                                              "message": "connection refused"}), "NETWORK")
        self.assertEqual(CM.classify_failure({"code": "E",
                                              "message": "empty response"}), "EMPTY")
        self.assertEqual(CM.classify_failure({"code": "E",
                                              "message": "truncated"}), "TRUNCATED")
        self.assertEqual(CM.classify_failure({"code": "X", "message": "weird"}), "OTHER")


# ---------------------------------------------------------------------------
# 9. 事件形状（契约 §7.5：只增，字段照契约）
# ---------------------------------------------------------------------------


class EventShapeTest(unittest.TestCase):
    def test_context_compacted_shape(self):
        g = CM.ContextGovernor(8000, summarizer=StubSummarizer())
        msgs = build_tool_session(n_tools=8, content="A" * 3000)
        g.ensure_before_dispatch(msgs)
        evts = g.drain_events()
        self.assertEqual(len(evts), 1)
        e = evts[0]
        self.assertEqual(e["event"], "context.compacted")
        data = e["data"]
        for key in ("level", "state", "estimatedTokens", "tokensAfter",
                    "llmCalls", "keptTailTokens", "summaryNote"):
            self.assertIn(key, data)
        self.assertEqual(data["llmCalls"], 0)
        self.assertEqual(data["keptTailTokens"], CM.tail_tokens(8000))
        self.assertEqual(data["summaryNote"], CM.SUMMARY_NOTE)
        self.assertIn(data["level"], ("L0", "L1", "L2", "L0|L1"))

    def test_mount2_event_before_retry(self):
        g = CM.ContextGovernor(3000, summarizer=StubSummarizer())
        msgs = build_chat_session(n_rounds=20)
        d = g.on_request_error(msgs, {"code": "CONTEXT_WINDOW_EXCEEDED",
                                      "message": "too long"})
        evts = g.drain_events()
        self.assertEqual(d["action"], "compact_retry")
        self.assertEqual(len(evts), 1)
        self.assertEqual(evts[0]["data"]["trigger"], "context-overflow")


# ---------------------------------------------------------------------------
# 10. 挂载点一全流程：未达触发点直接走
# ---------------------------------------------------------------------------


class PreStepTest(unittest.TestCase):
    def test_below_trigger_no_touch(self):
        g = CM.ContextGovernor(100000, summarizer=StubSummarizer())
        msgs = build_chat_session(n_rounds=2)
        out, changed = g.ensure_before_dispatch(msgs)
        self.assertFalse(changed)
        self.assertEqual(out, msgs)
        self.assertEqual(g.state, "IDLE")
        self.assertEqual(g.drain_events(), [])

    def test_big_tool_output_never_pays_llm(self):
        # 验收 3：纯工具输出超量场景不触发付费摘要
        stub = StubSummarizer()
        g = CM.ContextGovernor(8000, summarizer=stub, spill_dir=None)
        msgs = build_tool_session(n_tools=10, content="T" * 4000)
        out, changed = g.ensure_before_dispatch(msgs)
        self.assertEqual(stub.calls, 0)
        self.assertEqual(g.llm_calls, 0)
        self.assertTrue(changed)
        self.assertLess(CM.estimate_messages_tokens(out),
                        CM.estimate_messages_tokens(msgs))


# ---------------------------------------------------------------------------
# 11. reset（手动重试出口）
# ---------------------------------------------------------------------------


class ResetTest(unittest.TestCase):
    def test_tripped_reset_to_idle(self):
        g = CM.ContextGovernor(3000, summarizer=None)
        big = [{"role": "user", "content": "B" * 60000}]
        g.on_request_error(big, {"code": "CONTEXT_WINDOW_EXCEEDED",
                                 "message": "x"})
        self.assertEqual(g.state, "TRIPPED")
        CM.reset(g)
        self.assertEqual(g.state, "IDLE")
        self.assertEqual(g.overflow_attempts, 0)


if __name__ == "__main__":
    unittest.main()
