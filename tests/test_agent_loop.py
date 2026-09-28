"""agent_loop.py 单测：全部依赖注入假实现，零真网络、零真 shell、零真数据库。"""
from __future__ import annotations

import ast
import json
import os
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "python"))

import agent_loop as AL  # noqa: E402
import guard  # noqa: E402
from agent_loop import LoopContext, LoopDeps, run  # noqa: E402
from reasoning_split import ReasoningSplitter  # noqa: E402


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


class FakeToolResult:
    def __init__(self, ok=True, content="tool-output", error_code=None, error_message=None):
        self.ok = ok
        self.content = content
        self.error_code = error_code
        self.error_message = error_message


class FakeAudit:
    def __init__(self):
        self.decisions = []
        self.tool_calls = []

    def log_decision(self, **kw):
        self.decisions.append(kw)
        return len(self.decisions)

    def log_tool_call(self, **kw):
        self.tool_calls.append(kw)


def scripted_llm(scripts):
    """scripts: 每次调用按顺序取一个事件列表；记录收到的 messages 快照。"""
    calls = []

    def stream(cfg, messages, **kw):
        calls.append({"messages": [dict(m) for m in messages], "kwargs": kw})
        idx = min(len(calls) - 1, len(scripts) - 1)
        return iter(scripts[idx])

    stream.calls = calls
    return stream


def text_script(text="你好", finish="stop"):
    return [{"type": "delta", "text": text},
            {"type": "done", "finishReason": finish, "usage": {"total_tokens": 5},
             "toolCalls": []}]


def tool_script(name, args_json, call_id="call_1"):
    return [{"type": "delta", "text": "我来查一下"},
            {"type": "done", "finishReason": "tool_calls", "usage": {"total_tokens": 7},
             "toolCalls": [{"id": call_id, "type": "function", "name": name,
                            "arguments": args_json}]}]


def make_deps(llm, *, decision=None, approval="allow_once", exec_result=None,
              audit=None, cancelled=lambda: False, declares_image=lambda m: True):
    execs = []

    def exec_tool(name, args, workspace_root):
        execs.append({"name": name, "args": args, "workspace_root": workspace_root})
        return exec_result or FakeToolResult()

    approvals = []

    def request_approval(payload):
        approvals.append(payload)
        return approval

    deps = LoopDeps(
        llm_stream=llm,
        request_approval=request_approval,
        load_model_config=lambda: FakeConfig(),
        exec_tool=exec_tool,
        judge=lambda name, args: (
            decision if decision is not None else FakeDecision()),
        audit=audit,
        is_cancelled=cancelled,
        tool_schemas=lambda: [{"type": "function", "function": {"name": "read_file"}}],
        model_declares_image=declares_image,
        splitter_factory=ReasoningSplitter,
    )
    deps._execs = execs          # 测试用侧信道
    deps._approvals = approvals
    return deps


def kinds(events):
    return [e["type"] for e in events]


class PlainRoundTest(unittest.TestCase):
    def test_single_text_round(self):
        llm = scripted_llm([text_script("你好世界")])
        deps = make_deps(llm)
        ctx = LoopContext(messages=[{"role": "user", "content": "hi"}])
        events = list(run(ctx, deps=deps))
        self.assertEqual(kinds(events), ["chat.delta", "chat.done"])
        self.assertEqual(events[0]["text"], "你好世界")
        done = events[-1]
        self.assertEqual(done["rounds"], 1)
        self.assertEqual(done["toolCallsRun"], 0)
        self.assertEqual(done["usage"], {"total_tokens": 5})
        # messages 尾部多了 assistant
        self.assertEqual(ctx.messages[-1]["role"], "assistant")
        self.assertEqual(ctx.messages[-1]["content"], "你好世界")

    def test_reasoning_tags_split_without_losing_chars(self):
        text = "前面<thinking>思考内容</thinking>后面还有"
        llm = scripted_llm([[{"type": "delta", "text": text},
                             {"type": "done", "finishReason": "stop", "toolCalls": []}]])
        deps = make_deps(llm)
        ctx = LoopContext(messages=[{"role": "user", "content": "hi"}])
        events = list(run(ctx, deps=deps))
        reasoning = "".join(e["text"] for e in events if e["type"] == "chat.reasoning")
        body = "".join(e["text"] for e in events if e["type"] == "chat.delta")
        self.assertEqual(reasoning, "思考内容")
        # 标签本身是分隔符、被消费掉；标签外的内容一字不差、顺序不变
        self.assertEqual(body, "前面后面还有")
        # 落库/回灌模型的那份正文必须只收分流后的 body —— 以前收的是原始
        # delta，于是 <thinking> 原文被当成正文存进 sessions.db，界面一重载
        # 整段思考就露在正文里（2026-09-24 现场）。
        self.assertEqual(ctx.messages[-1]["content"], "前面后面还有")
        self.assertNotIn("thinking", ctx.messages[-1]["content"])
        self.assertNotIn("<", ctx.messages[-1]["content"])

    def test_tag_split_across_chunks_still_keeps_body_clean(self):
        llm = scripted_llm([[{"type": "delta", "text": "<thin"},
                             {"type": "delta", "text": "king>想"},
                             {"type": "delta", "text": "了半天</think"},
                             {"type": "delta", "text": "ing>答案是 42"},
                             {"type": "done", "finishReason": "stop", "toolCalls": []}]])
        ctx = LoopContext(messages=[{"role": "user", "content": "hi"}])
        events = list(run(ctx, deps=make_deps(llm)))
        reasoning = "".join(e["text"] for e in events if e["type"] == "chat.reasoning")
        self.assertEqual(reasoning, "想了半天")
        # 标签被切在 chunk 边界上时，尾巴也要进正文，不能丢字
        self.assertEqual(ctx.messages[-1]["content"], "答案是 42")

    def test_reasoning_channel_event_passthrough(self):
        llm = scripted_llm([[{"type": "reasoning", "text": "独立思考"},
                             {"type": "delta", "text": "正文"},
                             {"type": "done", "finishReason": "stop", "toolCalls": []}]])
        events = list(run(LoopContext(messages=[{"role": "user", "content": "hi"}]),
                          deps=make_deps(llm)))
        self.assertEqual(kinds(events), ["chat.reasoning", "chat.delta", "chat.done"])
        self.assertEqual(events[0]["text"], "独立思考")


class ToolRoundTest(unittest.TestCase):
    def _two_round_llm(self, name="read_file", args_json='{"path":"a.txt"}'):
        return scripted_llm([tool_script(name, args_json), text_script("查完了")])

    def test_tool_round_then_final_answer(self):
        llm = self._two_round_llm()
        deps = make_deps(llm, decision=FakeDecision("L1", requires_approval=True,
                                                    can_always_allow=True, reason="write inside"))
        ctx = LoopContext(messages=[{"role": "user", "content": "读一下"}],
                          workspace_root="/ws", session_id="s1")
        events = list(run(ctx, deps=deps))
        self.assertEqual(kinds(events),
                         ["chat.delta", "tool.call", "approval.request", "tool.result",
                          "chat.delta", "chat.done"])
        done = events[-1]
        self.assertEqual(done["rounds"], 2)
        self.assertEqual(done["toolCallsRun"], 1)
        # messages 顺序：user → assistant(tool_calls) → tool → assistant
        roles = [m["role"] for m in ctx.messages]
        self.assertEqual(roles, ["user", "assistant", "tool", "assistant"])
        self.assertIn("tool_calls", ctx.messages[1])
        self.assertEqual(ctx.messages[1]["tool_calls"][0]["function"]["name"], "read_file")
        self.assertEqual(ctx.messages[2]["tool_call_id"], "call_1")
        # 事件里的 arguments 是 dict，不是字符串
        call_evt = next(e for e in events if e["type"] == "tool.call")
        self.assertEqual(call_evt["arguments"], {"path": "a.txt"})
        self.assertEqual(call_evt["level"], "dangerous")
        self.assertTrue(call_evt["requiresApproval"])
        # 第二轮请求里带上了工具结果
        second = llm.calls[1]["messages"]
        self.assertTrue(any(m["role"] == "tool" for m in second))

    def test_approval_payload_shape(self):
        llm = self._two_round_llm()
        deps = make_deps(llm, decision=FakeDecision("L3", True, False, "delete is always L3"))
        events = list(run(LoopContext(messages=[{"role": "user", "content": "x"}],
                                      workspace_root="/ws"), deps=deps))
        req = next(e for e in events if e["type"] == "approval.request")
        self.assertEqual(req["approvalId"], "apr_1")
        self.assertEqual(req["toolCallId"], "call_1")
        self.assertEqual(req["name"], "read_file")
        self.assertEqual(req["level"], "hardline")
        self.assertFalse(req["canAlwaysAllow"])
        self.assertEqual(deps._approvals[0]["approvalId"], "apr_1")

    def test_deny_skips_execution(self):
        llm = self._two_round_llm()
        deps = make_deps(llm, decision=FakeDecision("L2", True, False), approval="deny")
        ctx = LoopContext(messages=[{"role": "user", "content": "x"}], workspace_root="/ws")
        events = list(run(ctx, deps=deps))
        self.assertEqual(deps._execs, [])                     # 工具没被执行
        res = next(e for e in events if e["type"] == "tool.result")
        self.assertFalse(res["ok"])
        self.assertEqual(res["decision"], "deny")
        tool_msg = next(m for m in ctx.messages if m["role"] == "tool")
        self.assertIn("拒绝", tool_msg["content"])

    def test_invalid_approval_value_counts_as_deny(self):
        llm = self._two_round_llm()
        deps = make_deps(llm, decision=FakeDecision("L2", True, False), approval="maybe")
        events = list(run(LoopContext(messages=[{"role": "user", "content": "x"}],
                                      workspace_root="/ws"), deps=deps))
        self.assertEqual(deps._execs, [])
        res = next(e for e in events if e["type"] == "tool.result")
        self.assertEqual(res["decision"], "deny")

    def test_ordinary_call_auto_allowed_but_audited(self):
        llm = self._two_round_llm()
        audit = FakeAudit()
        deps = make_deps(llm, decision=FakeDecision("L0", False, False, "L0 auto-allow"),
                         audit=audit)
        events = list(run(LoopContext(messages=[{"role": "user", "content": "x"}],
                                      workspace_root="/ws", session_id="s9"), deps=deps))
        self.assertNotIn("approval.request", kinds(events))
        self.assertEqual(len(deps._approvals), 0)
        self.assertEqual(len(deps._execs), 1)                 # 直接执行了
        self.assertEqual(len(audit.decisions), 1)
        self.assertEqual(audit.decisions[0]["decision"], "allow_once")
        self.assertEqual(audit.decisions[0]["level"], "")
        self.assertEqual(audit.decisions[0]["session_id"], "s9")
        self.assertEqual(len(audit.tool_calls), 1)
        self.assertIsInstance(audit.tool_calls[0]["duration_ms"], int)

    def test_allow_always_reaches_audit(self):
        llm = self._two_round_llm()
        audit = FakeAudit()
        deps = make_deps(llm, decision=FakeDecision("L1", True, True), approval="allow_always",
                         audit=audit)
        list(run(LoopContext(messages=[{"role": "user", "content": "x"}],
                             workspace_root="/ws"), deps=deps))
        self.assertEqual(audit.decisions[0]["decision"], "allow_always")
        self.assertEqual(len(deps._execs), 1)

    def test_bad_arguments_do_not_execute(self):
        llm = scripted_llm([tool_script("read_file", "{not json"),
                            text_script("好的")])
        deps = make_deps(llm)
        ctx = LoopContext(messages=[{"role": "user", "content": "x"}], workspace_root="/ws")
        events = list(run(ctx, deps=deps))
        self.assertEqual(deps._execs, [])
        res = next(e for e in events if e["type"] == "tool.result")
        self.assertFalse(res["ok"])
        self.assertEqual(res["errorCode"], "TOOL_ARGUMENTS_INVALID")
        tool_msg = next(m for m in ctx.messages if m["role"] == "tool")
        self.assertIn("JSON", tool_msg["content"])

    def test_duplicate_tool_call_aborts(self):
        same = tool_script("read_file", '{"path":"a.txt"}')
        llm = scripted_llm([same, same, same])
        deps = make_deps(llm)
        events = list(run(LoopContext(messages=[{"role": "user", "content": "x"}],
                                      workspace_root="/ws"), deps=deps))
        self.assertEqual(events[-1]["type"], "chat.error")
        self.assertEqual(events[-1]["code"], "DUPLICATE_TOOL")

    def test_loop_limit(self):
        scripts = [tool_script("read_file", '{"path":"f%d.txt"}' % i, "c%d" % i)
                   for i in range(10)]
        llm = scripted_llm(scripts)
        deps = make_deps(llm)
        ctx = LoopContext(messages=[{"role": "user", "content": "x"}], workspace_root="/ws",
                          max_loop=3)
        events = list(run(ctx, deps=deps))
        self.assertEqual(events[-1]["type"], "chat.error")
        self.assertEqual(events[-1]["code"], "LOOP_LIMIT")
        self.assertEqual(len(llm.calls), 3)
        self.assertIn("最大循环次数", ctx.messages[-1]["content"])

    def test_tool_output_truncated(self):
        llm = self._two_round_llm()
        big = "x" * (AL.MAX_TOOL_OUTPUT_CHARS + 500)
        deps = make_deps(llm, exec_result=FakeToolResult(True, big))
        ctx = LoopContext(messages=[{"role": "user", "content": "x"}], workspace_root="/ws")
        events = list(run(ctx, deps=deps))
        res = next(e for e in events if e["type"] == "tool.result")
        self.assertLess(len(res["content"]), len(big))
        self.assertIn("truncated", res["content"])
        tool_msg = next(m for m in ctx.messages if m["role"] == "tool")
        self.assertIn("truncated", tool_msg["content"])

    def test_failed_tool_result_reported_but_loop_continues(self):
        llm = self._two_round_llm()
        deps = make_deps(llm, exec_result=FakeToolResult(False, "boom", "NONZERO_EXIT", "exit 3"))
        events = list(run(LoopContext(messages=[{"role": "user", "content": "x"}],
                                      workspace_root="/ws"), deps=deps))
        res = next(e for e in events if e["type"] == "tool.result")
        self.assertFalse(res["ok"])
        self.assertEqual(res["errorCode"], "NONZERO_EXIT")
        self.assertEqual(events[-1]["type"], "chat.done")     # 循环继续到模型收尾


class GateAndErrorTest(unittest.TestCase):
    def test_image_gate_blocks_before_any_request(self):
        llm = scripted_llm([text_script("不会被调用")])
        deps = make_deps(llm, declares_image=lambda m: False)
        msgs = [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
            {"type": "text", "text": "这是什么"},
        ]}]
        events = list(run(LoopContext(messages=msgs), deps=deps))
        self.assertEqual(events[-1]["type"], "chat.error")
        self.assertEqual(events[-1]["code"], "MODEL_NO_IMAGE_INPUT")
        self.assertEqual(len(llm.calls), 0)                   # 一个请求都没发
        self.assertIn("fake-model", events[-1]["message"])

    def test_image_gate_passes_block_through(self):
        llm = scripted_llm([text_script("是一只猫")])
        deps = make_deps(llm, declares_image=lambda m: True)
        msgs = [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
            {"type": "text", "text": "这是什么"},
        ]}]
        events = list(run(LoopContext(messages=msgs), deps=deps))
        self.assertEqual(events[-1]["type"], "chat.done")
        sent = llm.calls[0]["messages"][0]["content"]
        self.assertEqual(sent[0]["type"], "image_url")
        self.assertTrue(sent[0]["image_url"]["url"].startswith("data:image/png;base64,"))
        self.assertEqual(sent[1]["text"], "这是什么")

    def test_llm_error_passthrough(self):
        llm = scripted_llm([[{"type": "error", "code": "AUTH", "message": "bad key"}]])
        events = list(run(LoopContext(messages=[{"role": "user", "content": "x"}]),
                          deps=make_deps(llm)))
        self.assertEqual(events[-1]["type"], "chat.error")
        self.assertEqual(events[-1]["code"], "AUTH")
        self.assertEqual(events[-1]["message"], "bad key")

    def test_cancel_yields_done_cancelled(self):
        llm = scripted_llm([text_script("半截")])
        # 假 llm 不消费 is_cancelled，所以循环只在流结束后检查一次 —— 直接判真
        events = list(run(LoopContext(messages=[{"role": "user", "content": "x"}]),
                          deps=make_deps(llm, cancelled=lambda: True)))
        self.assertEqual(events[-1]["type"], "chat.done")
        self.assertEqual(events[-1]["finishReason"], "cancelled")

    def test_unexpected_exception_becomes_internal_error(self):
        def exploding(cfg, messages, **kw):
            raise RuntimeError("boom")

        deps = make_deps(exploding)
        events = list(run(LoopContext(messages=[{"role": "user", "content": "x"}]), deps=deps))
        self.assertEqual(events[-1]["type"], "chat.error")
        self.assertEqual(events[-1]["code"], "INTERNAL")
        self.assertIn("boom", events[-1]["message"])

    def test_model_config_failure_is_config_error(self):
        class Boom(Exception):
            code = "CONFIG_ERROR"

        def bad_config():
            raise Boom("环境变量没设")

        deps = make_deps(scripted_llm([text_script()]))
        deps.load_model_config = bad_config
        events = list(run(LoopContext(messages=[{"role": "user", "content": "x"}]), deps=deps))
        self.assertEqual(events[-1]["type"], "chat.error")
        self.assertEqual(events[-1]["code"], "CONFIG_ERROR")


class SystemPromptTest(unittest.TestCase):
    def test_inserted_when_absent(self):
        llm = scripted_llm([text_script()])
        ctx = LoopContext(messages=[{"role": "user", "content": "hi"}],
                          system_prompt="你是助手")
        list(run(ctx, deps=make_deps(llm)))
        self.assertEqual(ctx.messages[0]["role"], "system")
        self.assertEqual(ctx.messages[0]["content"], "你是助手")

    def test_not_duplicated_when_present(self):
        llm = scripted_llm([text_script()])
        ctx = LoopContext(messages=[{"role": "system", "content": "已有"},
                                    {"role": "user", "content": "hi"}],
                          system_prompt="你是助手")
        list(run(ctx, deps=make_deps(llm)))
        sys_msgs = [m for m in ctx.messages if m["role"] == "system"]
        self.assertEqual(len(sys_msgs), 1)
        self.assertEqual(sys_msgs[0]["content"], "已有")


class ArchitectureTest(unittest.TestCase):
    def test_agent_loop_does_not_import_sidecar(self):
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "python", "agent_loop.py")
        with open(path, encoding="utf-8") as f:
            tree = ast.parse(f.read())
        modules = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                modules.add(node.module or "")
        self.assertNotIn("sidecar", modules)
        self.assertFalse(any((m or "").startswith("sidecar") for m in modules),
                         "agent_loop 不许 import sidecar：%s" % sorted(modules))

    def test_no_print_in_source(self):
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "python", "agent_loop.py")
        with open(path, encoding="utf-8") as f:
            src = f.read()
        self.assertNotIn("\nprint(", src)

    def test_default_deps_wires_real_modules(self):
        deps = AL.default_deps(request_approval=lambda p: "deny")
        self.assertTrue(callable(deps.llm_stream))
        self.assertTrue(callable(deps.exec_tool))
        self.assertTrue(callable(deps.judge))


class PromisesActionTest(unittest.TestCase):
    """「说了要做」的识别：只认第一人称承诺，不认已完成的报告。"""

    def test_matches_the_real_transcript(self):
        # 真机原文（2026-09-24）：「好，我去查。让我跑几个搜索试试。」→ 零工具调用
        self.assertTrue(AL.promises_action("好，我去查。Jev 可能是哪个模型的拼写，让我跑几个搜索试试。"))

    def test_matches_common_promises(self):
        for text in ("我去联网查一下。", "让我试试看。", "我先查一下文档。",
                     "我来搜搜这个模型。", "稍等，我去看看。", "我去找找相关资料。"):
            self.assertTrue(AL.promises_action(text), text)

    def test_ignores_completed_reports(self):
        for text in ("我查了，是 GLM-4.6。", "我看过那份文件了，里面写着 16:00。",
                     "我试过了，连不上。", "我搜到了三条结果。"):
            self.assertFalse(AL.promises_action(text), text)

    def test_ignores_plain_answers(self):
        for text in ("深圳今天晴，湿度 71%。", "这个文件有 130 行。", ""):
            self.assertFalse(AL.promises_action(text), text)

    def test_only_the_tail_is_inspected(self):
        # 前半段叙述「我去查了…」是回顾，结论已给，不该触发纠偏
        long_text = "我去查了一下发布说明，确认是三月底。" + "补充说明。" * 30
        self.assertFalse(AL.promises_action(long_text))


class ActionNudgeTest(unittest.TestCase):
    def test_nudge_fires_once_then_stops(self):
        # 第一轮：只说不做 → 纠偏一次；第二轮脚本仍是同样文本 → 预算用尽，正常结束
        llm = scripted_llm([text_script("好，我去查。让我跑几个搜索试试。")])
        deps = make_deps(llm)
        ctx = LoopContext(messages=[{"role": "user", "content": "你自己去联网查一下"}])
        events = list(run(ctx, deps=deps))
        self.assertEqual(len(llm.calls), 2)                      # 多跑了一轮
        done = [e for e in events if e["type"] == "chat.done"][-1]
        self.assertEqual(done["rounds"], 2)
        # 纠偏消息必须以 system 角色进入上下文：chat_persist 不落 system 行，
        # 所以它进模型但不进会话库（界面上不会冒出一条假消息）。
        injected = [m for m in llm.calls[1]["messages"] if m.get("role") == "system"]
        self.assertEqual(len(injected), 1)
        self.assertIn("没有发出任何工具调用", injected[0]["content"])

    def test_no_nudge_when_the_model_actually_called_a_tool(self):
        llm = scripted_llm([tool_script("read_file", '{"path": "a.txt"}'), text_script("读完了")])
        deps = make_deps(llm)
        ctx = LoopContext(messages=[{"role": "user", "content": "看看 a.txt"}])
        list(run(ctx, deps=deps))
        self.assertEqual(len(llm.calls), 2)                      # 正常两轮，没有第三次
        self.assertFalse(any(m.get("role") == "system" and "没有发出任何工具调用" in str(m.get("content"))
                             for call in llm.calls for m in call["messages"]))

    def test_no_nudge_for_a_plain_answer(self):
        llm = scripted_llm([text_script("深圳今天晴，湿度 71%。")])
        deps = make_deps(llm)
        ctx = LoopContext(messages=[{"role": "user", "content": "天气"}])
        list(run(ctx, deps=deps))
        self.assertEqual(len(llm.calls), 1)

    def test_budget_is_shared_across_the_run(self):
        # 两轮都只说不做：只能纠一次，第二轮直接收尾（不能无限自纠）
        llm = scripted_llm([text_script("让我去查一下。")])
        deps = make_deps(llm)
        ctx = LoopContext(messages=[{"role": "user", "content": "查"}])
        events = list(run(ctx, deps=deps))
        self.assertEqual(len(llm.calls), 2)
        self.assertEqual(len([e for e in events if e["type"] == "chat.done"]), 1)

    def test_nudge_does_not_leak_into_the_assistant_text(self):
        llm = scripted_llm([text_script("好，我去查。")])
        deps = make_deps(llm)
        ctx = LoopContext(messages=[{"role": "user", "content": "查"}])
        events = list(run(ctx, deps=deps))
        body = "".join(e["text"] for e in events if e["type"] == "chat.delta")
        self.assertNotIn("停一下", body)


if __name__ == "__main__":
    unittest.main()

def guard_deps(llm, *, audit=None, approval="allow_once", exec_result=None,
               cancelled=lambda: False):
    """和 make_deps 一样，但 judge 用真的 guard.judge（新口径端到端）。"""
    execs, approvals = [], []

    def exec_tool(name, args, workspace_root):
        execs.append({"name": name, "args": args, "workspace_root": workspace_root})
        return exec_result or FakeToolResult()

    def request_approval(payload):
        approvals.append(payload)
        return approval

    deps = LoopDeps(
        llm_stream=llm,
        request_approval=request_approval,
        load_model_config=lambda: FakeConfig(),
        exec_tool=exec_tool,
        judge=lambda name, args: guard.judge(name, args),
        audit=audit,
        is_cancelled=cancelled,
        tool_schemas=lambda: [{"type": "function", "function": {"name": "run_shell"}}],
        model_declares_image=lambda m: True,
        splitter_factory=ReasoningSplitter,
    )
    deps._execs = execs
    deps._approvals = approvals
    return deps


class NewPermissionFlowTest(unittest.TestCase):
    """只有命令模式会拦；其余一律放行。"""

    def _two_round(self, command_json):
        return scripted_llm([tool_script("run_shell", command_json), text_script("好了")])

    def _ctx(self, ws="/ws"):
        return LoopContext(messages=[{"role": "user", "content": "跑个命令"}],
                           workspace_root=ws, session_id="s-perm")

    def test_deps_has_no_rules_or_always_allow_fields(self):
        """分级与「总是允许」的接线必须彻底消失。"""
        deps = guard_deps(self._two_round('{"command": "ls"}'))
        for gone in ("load_rules", "rule_for_approval", "save_rule"):
            self.assertFalse(hasattr(deps, gone), "%s 不该还在 LoopDeps 上" % gone)

    def test_ordinary_command_runs_silently_and_is_audited(self):
        llm = self._two_round('{"command": "git status"}')
        audit = FakeAudit()
        deps = guard_deps(llm, audit=audit)
        events = list(run(self._ctx(), deps=deps))
        self.assertNotIn("approval.request", kinds(events))
        self.assertEqual(len(deps._execs), 1)
        self.assertEqual(audit.decisions[0]["level"], "")            # tier 空 = 放行
        self.assertEqual(audit.decisions[0]["arguments"], {"command": "git status"})

    def test_workdir_does_not_change_the_verdict(self):
        """基准目录不再参与判定：同一个普通命令，换目录结果必须一样。"""
        for ws in ("/ws", "/tmp", "C:\\other", "/home/someone"):
            llm = self._two_round('{"command": "ls -la"}')
            deps = guard_deps(llm)
            events = list(run(self._ctx(ws), deps=deps))
            self.assertNotIn("approval.request", kinds(events), ws)
            self.assertEqual(len(deps._execs), 1, ws)

    def test_dangerous_command_asks_with_tier(self):
        llm = self._two_round('{"command": "rm -rf ./build"}')
        deps = guard_deps(llm)
        events = list(run(self._ctx(), deps=deps))
        req = next(e for e in events if e["type"] == "approval.request")
        self.assertEqual(req["level"], "dangerous")
        self.assertFalse(req["canAlwaysAllow"])                      # 没有「总是允许」
        call = next(e for e in events if e["type"] == "tool.call")
        self.assertEqual(call["level"], "dangerous")

    def test_hardline_command_asks_with_tier(self):
        llm = self._two_round('{"command": "rm -rf /"}')
        deps = guard_deps(llm)
        events = list(run(self._ctx(), deps=deps))
        req = next(e for e in events if e["type"] == "approval.request")
        self.assertEqual(req["level"], "hardline")
        self.assertFalse(req["canAlwaysAllow"])

    def test_same_dangerous_command_asks_every_time(self):
        """没有「总是允许」：同一条危险命令下一轮照样问。"""
        for _ in range(2):
            llm = self._two_round('{"command": "rm -rf ./dist"}')
            deps = guard_deps(llm)
            events = list(run(self._ctx(), deps=deps))
            self.assertIn("approval.request", kinds(events))

    def test_denied_dangerous_command_is_not_executed(self):
        llm = self._two_round('{"command": "rm -rf /"}')
        deps = guard_deps(llm, approval="deny")
        events = list(run(self._ctx(), deps=deps))
        self.assertEqual(deps._execs, [])
        res = next(e for e in events if e["type"] == "tool.result")
        self.assertEqual(res["decision"], "deny")
