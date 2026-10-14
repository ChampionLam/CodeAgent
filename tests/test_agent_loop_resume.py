"""断点/断网退避的 loop 侧单测：报了什么断点、网络错误重试几次、放弃时是可续状态。"""

from __future__ import annotations

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "python"))

import agent_loop as AL  # noqa: E402
from agent_loop import LoopContext, LoopDeps, run  # noqa: E402
from reasoning_split import ReasoningSplitter  # noqa: E402

# 退避等待在测试里要快：把基准压到毫秒级
AL.NET_RETRY_BASE = 0.01


class FakeConfig:
    model = "fake-model"
    max_tokens = 128


class FakeDecision:
    level = "L2"
    requires_approval = False
    can_always_allow = False
    reason = "test"


class FakeToolResult:
    def __init__(self, ok=True, content="out", error_code=None, error_message=None):
        self.ok = ok
        self.content = content
        self.error_code = error_code
        self.error_message = error_message


class CheckpointRecorder:
    def __init__(self):
        self.events = []

    def __call__(self, payload):
        self.events.append(dict(payload))


def stream_from(errors, then=None):
    """先按顺序吐 error，再（可选）走正常一轮。"""
    def stream(cfg, messages, **kw):
        for code in list(errors):
            yield {"type": "error", "code": code, "message": "连接被重置"}
        if then is False:
            return
        yield {"type": "done", "finishReason": "stop", "usage": {}, "toolCalls": []}
        yield {"type": "delta", "text": "好了"}
    return stream


def make_deps(llm_stream, checkpoint=None, run_id="r-test", governor=None):
    deps = LoopDeps(
        llm_stream=llm_stream,
        context_governor=governor,
        request_approval=lambda payload: "allow_once",
        load_model_config=lambda: FakeConfig(),
        exec_tool=lambda name, args, workspace_root: FakeToolResult(),
        judge=lambda name, args: FakeDecision(),
        audit=None,
        is_cancelled=lambda: False,
        tool_schemas=lambda: [],
        model_declares_image=lambda m: True,
        splitter_factory=ReasoningSplitter,
        run_id=run_id,
        checkpoint=checkpoint,
    )
    return deps


def ctx():
    return LoopContext(messages=[{"role": "user", "content": "hi"}], max_loop=4)


class CheckpointReportTest(unittest.TestCase):
    def test_reports_round_and_end_done(self):
        rec = CheckpointRecorder()
        events = list(run(ctx(), deps=make_deps(stream_from([]), checkpoint=rec)))
        kinds = [e["event"] for e in rec.events]
        self.assertIn("round", kinds)
        self.assertEqual(rec.events[-1]["event"], "end")
        self.assertEqual(rec.events[-1]["status"], "done")
        self.assertEqual(rec.events[0]["run_id"], "r-test", "断点要带运行号")
        self.assertEqual(events[-1]["type"], AL.EVENT_DONE)

    def test_reports_end_failed_on_hard_error(self):
        rec = CheckpointRecorder()

        def stream(cfg, messages, **kw):
            yield {"type": "error", "code": "PROVIDER_ERROR", "message": "400 参数不对"}

        list(run(ctx(), deps=make_deps(stream, checkpoint=rec)))
        self.assertEqual(rec.events[-1]["status"], "failed")
        self.assertEqual(rec.events[-1]["reason"], "PROVIDER_ERROR")

    def test_checkpoint_failure_never_breaks_the_turn(self):
        class Boom:
            def __call__(self, payload):
                raise RuntimeError("disk full")

        events = list(run(ctx(), deps=make_deps(stream_from([]), checkpoint=Boom())))
        self.assertEqual(events[-1]["type"], AL.EVENT_DONE)

    def test_no_checkpoint_wired_is_fine(self):
        events = list(run(ctx(), deps=make_deps(stream_from([]))))
        self.assertEqual(events[-1]["type"], AL.EVENT_DONE)


class PartialTextCheckpointTest(unittest.TestCase):
    """「写到一半的正文」要边流边存。

    起因（2026-09-25 真机实测）：点中断/关应用时主进程立刻杀 sidecar，循环走不到
    「把这一轮文字落会话库」那一步，半截回答就没了，续跑时模型一脸茫然。
    """

    def test_partial_is_reported_while_streaming(self):
        rec = CheckpointRecorder()

        def stream(cfg, messages, **kw):
            yield {"type": "delta", "text": "我先看了前八页，"}
            yield {"type": "delta", "text": "内容是长寿村火蝉"}
            yield {"type": "done", "finishReason": "stop", "usage": {}, "toolCalls": []}

        list(run(ctx(), deps=make_deps(stream, checkpoint=rec)))
        partials = [e for e in rec.events if e.get("event") == "partial"]
        self.assertTrue(partials, "流里就该报一次半截正文")
        self.assertIn("我先看了前八页", partials[0]["text"])
        self.assertEqual(partials[0]["run_id"], "r-test")

    def test_partial_flushed_when_cancelled(self):
        rec = CheckpointRecorder()
        state = {"cancel": False}

        def stream(cfg, messages, **kw):
            yield {"type": "delta", "text": "正在写攻略正文"}
            state["cancel"] = True          # 用户此刻点了取消
            yield {"type": "done", "finishReason": "stop", "usage": {}, "toolCalls": []}

        deps = make_deps(stream, checkpoint=rec)
        deps.is_cancelled = lambda: state["cancel"]
        list(run(ctx(), deps=deps))
        partials = [e for e in rec.events if e.get("event") == "partial"]
        self.assertTrue(any("攻略正文" in e["text"] for e in partials),
                        "取消前必须把半截正文存下来")

    def test_partial_flushed_early_when_big_chunks_pour_in(self):
        """快流：每块都很大时不能傻等 2 秒，攒够字数就该落盘。"""
        rec = CheckpointRecorder()

        def stream(cfg, messages, **kw):
            for i in range(3):
                yield {"type": "delta", "text": "第%d段" % i + "内容" * 400}
            yield {"type": "done", "finishReason": "stop", "usage": {}, "toolCalls": []}

        list(run(ctx(), deps=make_deps(stream, checkpoint=rec)))
        partials = [e for e in rec.events if e.get("event") == "partial"]
        self.assertGreaterEqual(len(partials), 2, "第二块攒够字数就该再存一次")
        self.assertIn("第1段", partials[1]["text"])

    def test_long_partial_is_capped(self):
        rec = CheckpointRecorder()

        def stream(cfg, messages, **kw):
            yield {"type": "delta", "text": "字" * 5000}
            yield {"type": "done", "finishReason": "stop", "usage": {}, "toolCalls": []}

        list(run(ctx(), deps=make_deps(stream, checkpoint=rec)))
        partials = [e for e in rec.events if e.get("event") == "partial"]
        self.assertTrue(partials)
        self.assertLessEqual(len(partials[0]["text"]), AL.PARTIAL_MAX)

    def test_nothing_written_no_partial(self):
        rec = CheckpointRecorder()

        def stream(cfg, messages, **kw):
            yield {"type": "done", "finishReason": "stop", "usage": {}, "toolCalls": []}

        list(run(ctx(), deps=make_deps(stream, checkpoint=rec)))
        self.assertFalse([e for e in rec.events if e.get("event") == "partial"])


class NetRetryTest(unittest.TestCase):
    def test_transient_network_error_is_retried_and_then_succeeds(self):
        rec = CheckpointRecorder()
        attempts = {"n": 0}

        def stream(cfg, messages, **kw):
            attempts["n"] += 1
            if attempts["n"] == 1:
                yield {"type": "error", "code": "NETWORK", "message": "连接被重置"}
                return
            yield {"type": "delta", "text": "恢复了"}
            yield {"type": "done", "finishReason": "stop", "usage": {}, "toolCalls": []}

        events = list(run(ctx(), deps=make_deps(stream, checkpoint=rec)))
        self.assertEqual(attempts["n"], 2, "断网要自己重试一次")
        notices = [e for e in events if e["type"] == AL.EVENT_NOTICE]
        self.assertTrue(notices, "要告诉用户「正在重试」")
        self.assertEqual(events[-1]["type"], AL.EVENT_DONE, "恢复后这一轮要正常收尾")
        self.assertNotIn("interrupt", [e["event"] for e in rec.events])

    def test_slow_timeout_is_still_retried(self):
        """慢调用（读超时）之后必须还有一次自动重试。

        2026-10-14 真机缺陷：重试窗口从**本轮开始**起算（旧 NET_RETRY_WINDOW=120s），
        而单次读超时默认 180s —— 第一次超时就让 elapsed(180) > 窗口(120)，
        于是 `elapsed < NET_RETRY_WINDOW` 不成立、重试被跳过。用户看到的就是
        「特别容易超时」：一次超时 = 立刻中断落断点，提示词里写的「自动重试 2-3 次」
        对超时类故障从来没发生过。

        这里把窗口压到 0.05s、让第 1 次尝试先耗 0.1s 再报 NETWORK（模拟慢的那次）：
        窗口按「本轮已耗时」算 → 0.1 > 0.05，不重试（红）；
        窗口按「第一次出错之后」算 → ≈0 < 0.05，重试（绿）。
        """
        import time

        old_window = AL.NET_RETRY_WINDOW
        AL.NET_RETRY_WINDOW = 0.05
        self.addCleanup(setattr, AL, "NET_RETRY_WINDOW", old_window)
        rec = CheckpointRecorder()
        attempts = {"n": 0}

        def stream(cfg, messages, **kw):
            attempts["n"] += 1
            if attempts["n"] == 1:
                time.sleep(0.1)          # 慢调用：一次读超时就长这样
                yield {"type": "error", "code": "NETWORK",
                       "message": "TimeoutError: The read operation timed out"}
                return
            yield {"type": "delta", "text": "第二次连上了"}
            yield {"type": "done", "finishReason": "stop", "usage": {}, "toolCalls": []}

        events = list(run(ctx(), deps=make_deps(stream, checkpoint=rec)))
        self.assertEqual(attempts["n"], 2, "一次慢超时之后必须还有一次自动重试")
        self.assertEqual(events[-1]["type"], AL.EVENT_DONE, "重试成功要正常收尾")
        self.assertNotIn("interrupt", [e["event"] for e in rec.events],
                         "能重试成功就不该落成中断")

    def test_network_retry_survives_a_propagating_governor(self):
        """governor 在场时，断网重试也必须发生（2026-10-14 真机缺陷）。

        旧代码先问 context governor：它对**任何**非溢出错误都回
        {"action": "propagate"}，decision 非 None 就照它走 —— 于是那段断网重试
        永远执行不到。真机 A/B（隔离实例 + 停机桩模型）两臂都只发出 1 次模型
        请求，界面直接报「[错误] NETWORK: TimeoutError: timed out」+「上次任务未完成」，
        与用户截图一致；那时我把 NET_RETRY_WINDOW 从 120 改到 300，真机毫无变化，
        因为整段逻辑都在 governor 的 return 之后。

        修法：瞬时故障判定提到 governor 之前（网络故障不是上下文问题）。
        """

        class PropagatingGovernor:
            """真机 governor 的行为：非溢出错误一律 propagate。"""

            def on_request_error(self, messages, error):
                return {"action": "propagate"}

        rec = CheckpointRecorder()
        attempts = {"n": 0}

        def stream(cfg, messages, **kw):
            attempts["n"] += 1
            if attempts["n"] == 1:
                yield {"type": "error", "code": "NETWORK",
                       "message": "TimeoutError: The read operation timed out"}
                return
            yield {"type": "delta", "text": "重试后完成"}
            yield {"type": "done", "finishReason": "stop", "usage": {}, "toolCalls": []}

        events = list(run(ctx(), deps=make_deps(stream, checkpoint=rec,
                                                governor=PropagatingGovernor())))
        self.assertEqual(attempts["n"], 2,
                         "governor 在场也不能吞掉断网重试（真机两臂都只发了 1 次请求）")
        notices = [e for e in events if e["type"] == AL.EVENT_NOTICE]
        self.assertTrue(notices, "重试前要给用户一句「网络中断，N 秒后重试」")
        self.assertEqual(events[-1]["type"], AL.EVENT_DONE, "重试成功要正常收尾")
        self.assertNotIn("interrupt", [e["event"] for e in rec.events],
                         "能重试成功就不该落成中断")

    def test_gives_up_after_max_retries_and_marks_resumable(self):
        rec = CheckpointRecorder()
        attempts = {"n": 0}

        def stream(cfg, messages, **kw):
            attempts["n"] += 1
            yield {"type": "error", "code": "NETWORK", "message": "网络不可达"}

        events = list(run(ctx(), deps=make_deps(stream, checkpoint=rec)))
        self.assertEqual(attempts["n"], AL.NET_RETRY_MAX + 1, "重试上限 = 1 次原始 + N 次重试")
        self.assertEqual(events[-1]["type"], AL.EVENT_ERROR)
        self.assertIn("继续", events[-1]["message"], "要告诉用户怎么续")
        self.assertEqual(rec.events[-1]["event"], "end")
        self.assertEqual(rec.events[-1]["status"], "interrupted",
                         "网络拖到放弃 = 可续的中断，不是 failed")
        self.assertEqual(rec.events[-1]["reason"], "网络/接口异常")

    def test_rate_limited_run_becomes_resumable(self):
        """限流（429）拖到放弃也要给「继续」入口。

        实测（2026-09-25 真机）：OpenRouter 免费档限流时，错误码是 HTTP_ERROR、
        httpStatus=429 —— 只看错误码会判成 failed，于是提示词里写着「点继续即可」
        但界面上根本没有「继续」按钮。自相矛盾，必须按 httpStatus 判。
        """
        rec = CheckpointRecorder()

        def stream(cfg, messages, **kw):
            yield {"type": "error", "code": "HTTP_ERROR", "httpStatus": 429,
                   "message": "rate limit exceeded"}

        events = list(run(ctx(), deps=make_deps(stream, checkpoint=rec)))
        self.assertEqual(events[-1]["type"], AL.EVENT_ERROR)
        self.assertEqual(rec.events[-1]["event"], "end")
        self.assertEqual(rec.events[-1]["status"], "interrupted",
                         "限流是瞬时故障，要给「继续」入口")
        self.assertEqual(rec.events[-1]["reason"], "网络/接口异常")

    def test_server_5xx_becomes_resumable(self):
        rec = CheckpointRecorder()

        def stream(cfg, messages, **kw):
            yield {"type": "error", "code": "PROVIDER_ERROR", "httpStatus": 503,
                   "message": "上游 503"}

        list(run(ctx(), deps=make_deps(stream, checkpoint=rec)))
        self.assertEqual(rec.events[-1]["status"], "interrupted")

    def test_permanent_error_stays_failed(self):
        """401/404 这种重试也没用的，不给「继续」入口，免得用户白点。"""
        rec = CheckpointRecorder()

        def stream(cfg, messages, **kw):
            yield {"type": "error", "code": "AUTH_ERROR", "httpStatus": 401,
                   "message": "密钥不对"}

        list(run(ctx(), deps=make_deps(stream, checkpoint=rec)))
        self.assertEqual(rec.events[-1]["status"], "failed")
        self.assertEqual(rec.events[-1]["reason"], "AUTH_ERROR")

    def test_non_retryable_error_is_not_retried(self):
        rec = CheckpointRecorder()
        attempts = {"n": 0}

        def stream(cfg, messages, **kw):
            attempts["n"] += 1
            yield {"type": "error", "code": "PROVIDER_ERROR", "message": "401 密钥不对",
                   "httpStatus": 401}

        events = list(run(ctx(), deps=make_deps(stream, checkpoint=rec)))
        self.assertEqual(attempts["n"], 1, "密钥错重试一百次也没用，不许退避重试")
        self.assertEqual(events[-1]["type"], AL.EVENT_ERROR)

    def test_retry_does_not_eat_the_round_budget(self):
        """退避重试不算一轮，否则网络抖一下就把 max_loop 吃光。"""
        rec = CheckpointRecorder()
        state = {"n": 0}

        def stream(cfg, messages, **kw):
            state["n"] += 1
            if state["n"] <= 2:
                yield {"type": "error", "code": "NETWORK", "message": "抖动"}
                return
            yield {"type": "done", "finishReason": "stop", "usage": {}, "toolCalls": []}

        events = list(run(ctx(), deps=make_deps(stream, checkpoint=rec)))
        done = events[-1]
        self.assertEqual(done["type"], AL.EVENT_DONE)
        self.assertLessEqual(done["rounds"], 2, "两次网络抖动不该顶掉轮次预算")

    def test_server_5xx_is_retryable_too(self):
        attempts = {"n": 0}

        def stream(cfg, messages, **kw):
            attempts["n"] += 1
            if attempts["n"] == 1:
                yield {"type": "error", "code": "PROVIDER_ERROR", "message": "502",
                       "httpStatus": 502}
                return
            yield {"type": "done", "finishReason": "stop", "usage": {}, "toolCalls": []}

        events = list(run(ctx(), deps=make_deps(stream)))
        self.assertEqual(attempts["n"], 2)
        self.assertEqual(events[-1]["type"], AL.EVENT_DONE)

    def test_cancel_during_backoff_stops_quietly(self):
        state = {"n": 0, "cancelled": False}

        def stream(cfg, messages, **kw):
            state["n"] += 1
            yield {"type": "error", "code": "NETWORK", "message": "断网"}

        deps = make_deps(stream)
        deps.is_cancelled = lambda: state["cancelled"]
        AL.NET_RETRY_BASE = 0.5

        def cancel_soon():
            state["cancelled"] = True

        import threading
        t = threading.Timer(0.1, cancel_soon)
        t.start()
        events = list(run(ctx(), deps=deps))
        t.cancel()
        self.assertEqual(events[-1]["type"], AL.EVENT_DONE, "取消要安静收尾")
        self.assertEqual(events[-1]["finishReason"], "cancelled")


class ToolCheckpointTest(unittest.TestCase):
    def test_done_calls_are_reported(self):
        rec = CheckpointRecorder()

        def stream(cfg, messages, **kw):
            yield {"type": "done", "finishReason": "tool_calls", "usage": {},
                   "toolCalls": [{"id": "c1", "type": "function", "name": "run_shell",
                                  "arguments": json.dumps({"command": "ls"})}]}

        calls = {"n": 0}

        def stream2(cfg, messages, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                yield from stream(cfg, messages, **kw)
                return
            yield {"type": "done", "finishReason": "stop", "usage": {}, "toolCalls": []}

        list(run(ctx(), deps=make_deps(stream2, checkpoint=rec)))
        tools = [e for e in rec.events if e["event"] == "tools"]
        self.assertTrue(tools)
        self.assertEqual(tools[-1]["done_calls"], ["c1"], "执行完的调用要记下来（续跑不重做）")


if __name__ == "__main__":
    unittest.main()