"""Sessions RPC + chat persistence tests.

Two layers, deliberately separated:
  * the wire layer (sidecar.handle_request) for sessions.* / messages.search;
  * the persist layer (chat_persist) driven directly, so the chat-turn write
    path is tested without booting the whole model-config harness. The wiring
    itself is pinned by the static checks at the bottom.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "python"))

import chat_persist  # noqa: E402
import sessionstore  # noqa: E402
import sidecar  # noqa: E402

PHRASE = "今天天气不错适合骑车"


class RpcCase(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.mkdtemp(prefix="desk-sessions-rpc-")
        self.store = sessionstore.SessionStore(os.path.join(self.dir, "sessions.db"))
        self._saved = sidecar._SESSIONS
        sidecar._SESSIONS = self.store
        self._warnings: list[str] = []

    def tearDown(self) -> None:
        sidecar._SESSIONS = self._saved
        self.store.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def _call(self, method: str, **params):
        payload = {"protocolVersion": 1}
        payload.update(params)
        return sidecar.handle_request(
            {"type": "req", "id": "r1", "method": method, "params": payload})

    def _warn(self, level, msg):
        self._warnings.append("%s %s" % (level, msg))


class SessionsRpcTest(RpcCase):
    def test_create_list_rename_delete(self) -> None:
        created = self._call("sessions.create", title="第一会话", model="MiniMax-M3")
        self.assertTrue(created["ok"], created)
        sid = created["result"]["id"]

        listed = self._call("sessions.list")["result"]["sessions"]
        self.assertEqual([s["id"] for s in listed], [sid])
        self.assertEqual(listed[0]["title"], "第一会话")
        self.assertEqual(listed[0]["messageCount"], 0)

        renamed = self._call("sessions.rename", sessionId=sid, title="改过名")
        self.assertTrue(renamed["ok"], renamed)
        self.assertEqual(self.store.get_session(sid)["title"], "改过名")

        removed = self._call("sessions.delete", sessionId=sid)
        self.assertTrue(removed["ok"], removed)
        self.assertEqual(removed["result"]["removed"], sid)
        self.assertEqual(self._call("sessions.list")["result"]["sessions"], [])
        # Soft delete: the row is still there when asked for explicitly.
        self.assertEqual(len(self._call("sessions.list", includeDeleted=True)["result"]["sessions"]), 1)

    def test_create_honours_client_supplied_session_id(self) -> None:
        # The renderer mints an id before the first turn and keeps using it for
        # every later RPC, so the store has to adopt it rather than invent one.
        wanted = "s-from-ui-1"
        created = self._call("sessions.create", sessionId=wanted, title="界面建的")
        self.assertTrue(created["ok"], created)
        self.assertEqual(created["result"]["id"], wanted)
        listed = self._call("sessions.list")["result"]["sessions"]
        self.assertEqual([s["id"] for s in listed], [wanted])

    def test_create_without_session_id_still_mints_one(self) -> None:
        created = self._call("sessions.create", title="没带 id")
        self.assertTrue(created["ok"], created)
        self.assertTrue(created["result"]["id"])

    def test_messages_round_trip_through_the_wire(self) -> None:
        sid = self._call("sessions.create")["result"]["id"]
        self.store.append_message(sid, {"role": "user", "content": "你好"})
        self.store.append_message(sid, {"role": "assistant", "content": "你好，我是 CodeAgent"})
        got = self._call("sessions.messages", sessionId=sid)
        self.assertTrue(got["ok"], got)
        roles = [m["role"] for m in got["result"]["messages"]]
        self.assertEqual(roles, ["user", "assistant"])

    def test_search_hits_chinese_and_scopes_by_session(self) -> None:
        a = self._call("sessions.create", title="甲")["result"]["id"]
        b = self._call("sessions.create", title="乙")["result"]["id"]
        self.store.append_message(a, {"role": "user", "content": PHRASE})
        self.store.append_message(b, {"role": "assistant", "content": "他问" + PHRASE})

        all_hits = self._call("messages.search", query=PHRASE)["result"]["hits"]
        self.assertEqual({h["sessionId"] for h in all_hits}, {a, b})
        one = self._call("messages.search", query=PHRASE, sessionId=a)["result"]["hits"]
        self.assertEqual([h["sessionId"] for h in one], [a])

    def test_rename_missing_session_is_an_error_not_a_crash(self) -> None:
        res = self._call("sessions.rename", sessionId="nope", title="x")
        self.assertFalse(res["ok"], res)
        self.assertTrue(res["error"]["code"])

    def test_store_unavailable_says_so(self) -> None:
        sidecar._SESSIONS = False
        res = self._call("sessions.list")
        self.assertFalse(res["ok"], res)
        self.assertEqual(res["error"]["code"], "STORE_UNAVAILABLE")


class ChatPersistTest(RpcCase):
    def _open(self, messages):
        return chat_persist.open_chat_session(
            self.store, session_id=None, messages=messages,
            model="MiniMax-M3", base_url="https://api.minimaxi.com/v1", warn=self._warn)

    def test_new_session_is_created_and_user_turn_survives(self) -> None:
        request = [{"role": "user", "content": PHRASE + "，帮我看下文档"}]
        sid = self._open(request)
        self.assertTrue(sid)
        self.assertEqual(self.store.get_session(sid)["title"][:6], PHRASE[:6])
        chat_persist.persist_new_user_messages(self.store, sid, request, warn=self._warn)
        self.assertEqual([m["content"] for m in self.store.get_messages(sid)], [request[0]["content"]])

    def test_chat_result_writes_assistant_and_usage(self) -> None:
        request = [{"role": "user", "content": "读一下 a.txt"}]
        sid = self._open(request)
        chat_persist.persist_new_user_messages(self.store, sid, request, warn=self._warn)
        # The real loop view has the system prompt inserted at index 0; the
        # persistence tail is everything after the client's request prefix.
        loop_messages = ([{"role": "system", "content": "SYS"}] + request
                         + [{"role": "assistant", "content": "读完了"}])
        chat_persist.persist_chat_result(
            self.store, sid, loop_messages=loop_messages, request_messages=request,
            system_prompt="SYS", usage={"prompt_tokens": 120, "completion_tokens": 30,
                                        "prompt_tokens_details": {"cached_tokens": 80}},
            tool_events=[], model="MiniMax-M3",
            base_url="https://api.minimaxi.com/v1", warn=self._warn)
        rows = self.store.get_messages(sid)
        self.assertEqual([m["role"] for m in rows], ["user", "assistant"])
        self.assertEqual(rows[1]["content"], "读完了")
        session = self.store.get_session(sid)
        self.assertEqual(session["total_input_tokens"], 120)
        self.assertEqual(session["total_output_tokens"], 30)

    def test_tool_events_become_tool_call_rows(self) -> None:
        request = [{"role": "user", "content": "读 a.txt"}]
        sid = self._open(request)
        chat_persist.persist_new_user_messages(self.store, sid, request, warn=self._warn)
        capture: dict = {}
        chat_persist.merge_tool_event(capture, "tool.call", {
            "id": "call_1", "name": "read_file", "arguments": {"path": "a.txt"}, "level": "L0"})
        chat_persist.merge_tool_event(capture, "tool.result", {
            "id": "call_1", "name": "read_file", "ok": True, "content": "内容",
            "durationMs": 12, "decision": "allow_once"})
        loop_messages = [{"role": "system", "content": "SYS"}] + request + [
            {"role": "assistant", "content": None,
             "tool_calls": [{"id": "call_1", "type": "function",
                             "function": {"name": "read_file", "arguments": "{\"path\":\"a.txt\"}"}}]},
            {"role": "tool", "content": "内容", "tool_call_id": "call_1"},
            {"role": "assistant", "content": "读完了"},
        ]
        chat_persist.persist_chat_result(
            self.store, sid, loop_messages=loop_messages, request_messages=request,
            system_prompt="SYS", usage=None, tool_events=list(capture.values()),
            model="MiniMax-M3", base_url=None, warn=self._warn)
        count = self.store._conn.execute("SELECT COUNT(*) FROM tool_calls").fetchone()[0]
        self.assertEqual(count, 1)
        self.assertEqual([m["role"] for m in self.store.get_messages(sid)],
                         ["user", "assistant", "tool", "assistant"])

    def test_a_broken_store_never_kills_the_chat(self) -> None:
        class Exploding:
            def __getattr__(self, name):
                raise RuntimeError("disk on fire")

        sid = self._open([{"role": "user", "content": "hi"}])
        self.assertTrue(sid)
        # Every call must swallow the failure and warn; none may raise.
        chat_persist.persist_new_user_messages(Exploding(), sid,
                                               [{"role": "user", "content": "hi"}],
                                               warn=self._warn)
        chat_persist.persist_chat_result(
            Exploding(), sid, loop_messages=[{"role": "assistant", "content": "x"}],
            request_messages=[], system_prompt=None, usage=None, tool_events=[],
            model="m", base_url=None, warn=self._warn)
        self.assertTrue(self._warnings, "expected at least one warning")


class WiringTest(unittest.TestCase):
    """Pin the sidecar wiring that unit tests above drive directly."""

    def setUp(self) -> None:
        with open(os.path.join(REPO, "python", "sidecar.py"), encoding="utf-8") as fh:
            self.src = fh.read()

    def test_chat_path_persists_after_the_loop(self) -> None:
        self.assertIn("chat_persist.merge_tool_event(tool_capture", self.src)
        self.assertIn("chat_persist.persist_chat_result(", self.src)
        self.assertIn("chat_persist.open_chat_session(", self.src)

    def test_sessions_methods_are_whitelisted(self) -> None:
        for method in ("sessions.list", "sessions.create", "sessions.rename",
                       "sessions.delete", "sessions.messages", "messages.search"):
            self.assertIn('"%s"' % method, self.src)


if __name__ == "__main__":
    unittest.main()