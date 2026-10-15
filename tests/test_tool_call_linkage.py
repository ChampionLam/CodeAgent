r"""工具调用必须落库并挂到本轮的消息上（2026-10-14 真机）。

现象：会话库里 tool_calls 表**是空的**，日志里每条都是
    tool call <uuid>: no message to link, skipping row
根因：事件里的 id 和消息 tool_calls 里的 id 不是同一套口径 —— 事件 id 是
侧车/循环自己生成的 uuid，而消息里存的是模型给的 call id（桩模型是
call_stub_1，真实模型是 call_xxx）。按 id 硬对，永远对不上。

这里把「不依赖 id」的兜底钉住：callId / id / (工具名 + 规范化参数) / 单调用轮次。
"""
import json
import unittest

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "python"))

import chat_persist  # noqa: E402


class _Store:
    def __init__(self):
        self.rows = []
        self.warnings = []

    def append_tool_call(self, **kw):
        self.rows.append(kw)
        return {"id": kw["id"]}


def _warn(level, msg):
    return None


def _turn(call_id="call_stub_1", name="run_shell", args='{"command": "dir"}'):
    """本轮的消息尾部：assistant(带 tool_calls) + tool(结果) + assistant(收尾)。"""
    tail = [
        {"role": "assistant", "content": "",
         "tool_calls": [{"id": call_id, "type": "function",
                         "function": {"name": name, "arguments": args}}]},
        {"role": "tool", "tool_call_id": call_id, "content": "--- stdout ---\nok"},
        {"role": "assistant", "content": "跑完了。"},
    ]
    written = [{"id": "msg-A"}, {"id": "msg-B"}, {"id": "msg-C"}]
    return tail, written


class ToolCallLinkageTest(unittest.TestCase):

    def test_event_id_uuid_still_links_to_message(self):
        """事件 id 是 uuid、消息里是模型给的 call id —— 也要挂上（真机现状）。"""
        store = _Store()
        tail, written = _turn()
        events = [{"id": "e7e3838c-cee9-48d8-a5b6-7c8c1703d1d1", "name": "run_shell",
                   "arguments": {"command": "dir"}, "ok": True, "content": "ok",
                   "durationMs": 12}]
        chat_persist._persist_tool_calls(store, "s1", tail, written, events, _warn)
        self.assertEqual(len(store.rows), 1, "工具调用没落库")
        self.assertEqual(store.rows[0]["message_id"], "msg-A")
        self.assertEqual(store.rows[0]["tool_name"], "run_shell")

    def test_matching_id_still_works(self):
        """id 一致时走原来的快路径，行为不变。"""
        store = _Store()
        tail, written = _turn()
        events = [{"id": "call_stub_1", "name": "run_shell",
                   "arguments": {"command": "dir"}, "ok": False,
                   "errorCode": "AUTH", "errorMessage": "401"}]
        chat_persist._persist_tool_calls(store, "s1", tail, written, events, _warn)
        self.assertEqual(len(store.rows), 1)
        self.assertEqual(store.rows[0]["message_id"], "msg-A")
        self.assertEqual(store.rows[0]["error"]["code"], "AUTH")

    def test_multi_call_turn_links_by_name_and_args(self):
        """一轮里两个不同调用：按（工具名+参数）各挂各的。"""
        store = _Store()
        tail = [
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c1", "type": "function",
                 "function": {"name": "run_shell", "arguments": '{"command": "dir"}'}},
                {"id": "c2", "type": "function",
                 "function": {"name": "image_generate", "arguments": '{"prompt": "cat"}'}},
            ]},
            {"role": "tool", "tool_call_id": "c1", "content": "ok"},
            {"role": "tool", "tool_call_id": "c2", "content": "ok"},
            {"role": "assistant", "content": "都跑完了。"},
        ]
        written = [{"id": "msg-A"}, {"id": "msg-B"}, {"id": "msg-C"}, {"id": "msg-D"}]
        events = [
            {"id": "uuid-1", "name": "image_generate", "arguments": {"prompt": "cat"}, "ok": True},
            {"id": "uuid-2", "name": "run_shell", "arguments": {"command": "dir"}, "ok": True},
        ]
        chat_persist._persist_tool_calls(store, "s1", tail, written, events, _warn)
        by_name = {r["tool_name"]: r["message_id"] for r in store.rows}
        self.assertEqual(by_name.get("image_generate"), "msg-A")
        self.assertEqual(by_name.get("run_shell"), "msg-A")

    def test_no_candidate_still_skips_with_warning(self):
        """真的对不上（本轮没有工具调用消息）就别硬塞，仍打警告。"""
        store = _Store()
        tail = [{"role": "assistant", "content": "没调工具。"}]
        written = [{"id": "msg-A"}]
        events = [{"id": "uuid-9", "name": "run_shell", "arguments": {}, "ok": True},
                  {"id": "uuid-8", "name": "image_generate", "arguments": {}, "ok": True}]
        warns = []
        chat_persist._persist_tool_calls(store, "s1", tail, written, events,
                                        lambda lv, m: warns.append(m))
        self.assertEqual(store.rows, [])
        self.assertTrue(all("no message to link" in w for w in warns))
