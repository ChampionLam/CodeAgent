"""跨会话历史的出线字段：思考内容 + 工具调用行，一个都不能少。

教训（2026-09-25）：这两处都是「存了但不上线」——

  * `_message_wire` 不带 reasoning：思考明明落在库里，切回旧会话却看不到
    （用户看到的「思考过程」只活在流式那一瞬间）；
  * `tool_calls` 表只有 append 没有 read：历史里的工具卡永远是空的，
    模型的工具调用、结果、耗时全取不回来。

所以这里钉三件事：路由真的挂上了（不是 BAD_METHOD）、reasoning 在线上、
toolCalls 按 messageId 关联返回。
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "python"))

import sidecar  # noqa: E402

THINKING = "用户要我跑测试，先看看有哪些用例。"


class HistoryWireTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        # _DATA_DIR 是模块级常量（import 时已定），改环境变量太晚 —— 直接改常量
        # 并清掉缓存的 store，测试才不会写进仓库 data/。
        self._old_dir = sidecar._DATA_DIR
        self._old_store = getattr(sidecar, "_SESSIONS", None)
        sidecar._DATA_DIR = self._tmp.name
        sidecar._SESSIONS = None

    def tearDown(self):
        sidecar._SESSIONS = self._old_store
        sidecar._DATA_DIR = self._old_dir
        self._tmp.cleanup()

    def _seed(self) -> tuple[str, str]:
        """造一个会话：用户问、助手带思考 + 工具调用、工具回结果。"""
        store = sidecar._session_store()
        self.assertIsNotNone(store, "临时目录里的会话库没起来")
        sid = store.create_session(model="MiniMax-M3", title="历史用例")
        store.append_message(sid, {"role": "user", "content": "跑一下测试"})
        assistant = store.append_message(sid, {
            "role": "assistant",
            "content": "好的",
            "reasoning": THINKING,
            "tool_calls": [{"id": "call_1", "type": "function",
                            "function": {"name": "run_shell",
                                         "arguments": '{"command": "pytest -q"}'}}],
        })
        store.append_message(sid, {"role": "tool", "content": "3 passed",
                                   "tool_call_id": "call_1"})
        store.append_tool_call(id="call_1", session_id=sid, message_id=assistant["id"],
                               tool_name="run_shell",
                               arguments={"command": "pytest -q"},
                               permission_level="", decision="allow_once",
                               result={"stdout": "3 passed"}, duration_ms=4120)
        return sid, assistant["id"]

    def _messages(self, sid: str) -> dict:
        resp = sidecar.handle_request({
            "type": "req", "id": "h1", "method": "sessions.messages",
            "params": {"sessionId": sid,
                       "protocolVersion": sidecar.SIDECAR_PROTOCOL_VERSION},
        })
        self.assertNotEqual(resp.get("error", {}).get("code"), "BAD_METHOD",
                            "sessions.messages 没挂上路：%r" % (resp,))
        return resp.get("result") or {}

    def test_messages_rpc_is_routed(self):
        sid, _ = self._seed()
        result = self._messages(sid)
        self.assertEqual(result.get("sessionId"), sid)
        self.assertTrue(result.get("messages"), "历史一条消息都没回来")

    def test_reasoning_is_on_the_wire(self):
        sid, _ = self._seed()
        msgs = self._messages(sid)["messages"]
        assistant = [m for m in msgs if m.get("role") == "assistant"]
        self.assertTrue(assistant, "助手消息不在返回里")
        self.assertEqual(assistant[0].get("reasoning"), THINKING,
                         "思考内容又没出线：%r" % (assistant[0],))

    def test_tool_calls_come_back_keyed_by_message(self):
        sid, mid = self._seed()
        calls = self._messages(sid).get("toolCalls")
        self.assertTrue(calls, "toolCalls 没回传（tool_calls 表只有写没有读）")
        call = calls[0]
        self.assertEqual(call.get("messageId"), mid, "工具调用没关联到发起它的那条助手消息")
        self.assertEqual(call.get("toolName"), "run_shell")
        self.assertEqual(call.get("decision"), "allow_once")
        self.assertEqual(call.get("durationMs"), 4120)
        self.assertEqual((call.get("result") or {}).get("stdout"), "3 passed")

    def test_store_read_layer_round_trip(self):
        """读取层自己的往返：写一行、按 session_id 读回来，字段不许丢。"""
        sid, mid = self._seed()
        calls = sidecar._session_store().get_tool_calls(sid)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["messageId"], mid)
        self.assertEqual(calls[0]["arguments"], {"command": "pytest -q"})

    def test_tool_calls_are_scoped_to_their_session(self):
        """不许串会话：另一个会话读不到这个会话的工具调用。"""
        sid, _ = self._seed()
        other = sidecar._session_store().create_session(model="MiniMax-M3",
                                                       title="另一个会话")
        self.assertEqual(sidecar._session_store().get_tool_calls(other), [])
        self.assertEqual(len(sidecar._session_store().get_tool_calls(sid)), 1)

    def test_history_test_does_not_touch_repo_data(self):
        sid, _ = self._seed()
        self._messages(sid)
        self.assertTrue(os.path.exists(os.path.join(self._tmp.name, "sessions.db")),
                        "历史测试没落在临时目录，跑到仓库 data/ 去了")


if __name__ == "__main__":
    unittest.main()