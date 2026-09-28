"""Alignment tests for chat_persist.

The interesting cases are the ones a positional rule gets wrong: the client
re-sends user/assistant text only, while the session also holds tool rows the
backend wrote, and the loop's list can be shorter than what the client sent
once the middle has been summarized.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "python"))  # noqa: E402

import chat_persist  # noqa: E402
import sessionstore  # noqa: E402


def _tool_round(store, sid: str, question: str, answer: str) -> None:
    """A round that used a tool: four rows, two of which the client never sees again."""
    store.append_message(sid, {"role": "user", "content": question})
    store.append_message(sid, {
        "role": "assistant", "content": "",
        "tool_calls": [{"id": "call-1", "type": "function",
                        "function": {"name": "read_file", "arguments": "{}"}}]})
    store.append_message(sid, {"role": "tool", "tool_call_id": "call-1",
                               "content": "file body"})
    store.append_message(sid, {"role": "assistant", "content": answer})


class AlignCase(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.mkdtemp(prefix="desk-chat-persist-")
        self.store = sessionstore.SessionStore(os.path.join(self.dir, "sessions.db"))
        self.sid = self.store.create_session(model="MiniMax-M3", base_url=None, title="t")

    def tearDown(self) -> None:
        self.store.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def _rows(self):
        return self.store.get_messages(self.sid, include_inactive=True)

    def _texts(self):
        return [(r["role"], chat_persist._stored_text(r)) for r in self._rows()]


class NewTailTest(AlignCase):
    def test_new_user_message_lands_after_a_tool_round(self) -> None:
        _tool_round(self.store, self.sid, "问题一", "答案一")
        # What the client re-sends: no tool rows, no empty assistant carrier.
        incoming = [{"role": "user", "content": "问题一"},
                    {"role": "assistant", "content": "答案一"},
                    {"role": "user", "content": "问题二"}]

        chat_persist.persist_new_user_messages(self.store, self.sid, incoming)

        self.assertEqual(self._texts()[-1], ("user", "问题二"))
        self.assertEqual(len(self._rows()), 5)

    def test_second_call_with_the_same_history_writes_nothing(self) -> None:
        _tool_round(self.store, self.sid, "问题一", "答案一")
        incoming = [{"role": "user", "content": "问题一"},
                    {"role": "assistant", "content": "答案一"},
                    {"role": "user", "content": "问题二"}]

        chat_persist.persist_new_user_messages(self.store, self.sid, incoming)
        chat_persist.persist_new_user_messages(self.store, self.sid, incoming)

        self.assertEqual(len(self._rows()), 5)

    def test_repeated_identical_messages_still_append_once(self) -> None:
        self.store.append_message(self.sid, {"role": "user", "content": "继续"})
        self.store.append_message(self.sid, {"role": "assistant", "content": "好"})
        incoming = [{"role": "user", "content": "继续"},
                    {"role": "assistant", "content": "好"},
                    {"role": "user", "content": "继续"}]

        chat_persist.persist_new_user_messages(self.store, self.sid, incoming)

        self.assertEqual(self._texts()[-1], ("user", "继续"))
        self.assertEqual(len(self._rows()), 3)

    def test_system_prompt_is_ignored(self) -> None:
        self.store.append_message(self.sid, {"role": "user", "content": "你好"})
        incoming = [{"role": "system", "content": "你是助手"},
                    {"role": "user", "content": "你好"},
                    {"role": "user", "content": "在吗"}]

        chat_persist.persist_new_user_messages(self.store, self.sid, incoming)

        self.assertEqual(self._texts(), [("user", "你好"), ("user", "在吗")])


class ResultTailTest(AlignCase):
    def test_tail_after_last_turn_even_when_the_loop_list_is_compacted(self) -> None:
        request = [{"role": "user", "content": "问题二"}]
        loop = [{"role": "system", "content": "你是助手"},
                {"role": "user", "content": "[历史摘要]"},
                {"role": "assistant", "content": "摘要续写"},
                {"role": "user", "content": "问题二"},
                {"role": "assistant", "content": "答案二"}]

        chat_persist.persist_chat_result(
            self.store, self.sid,
            loop_messages=loop, request_messages=request,
            system_prompt="你是助手", usage=None, tool_events=[],
            model="MiniMax-M3", base_url=None)

        self.assertEqual(self._texts(), [("assistant", "答案二")])

    def test_tool_rows_of_this_round_land_with_their_tool_call_row(self) -> None:
        request = [{"role": "user", "content": "读文件"}]
        loop = [{"role": "system", "content": "你是助手"},
                {"role": "user", "content": "读文件"},
                {"role": "assistant", "content": "",
                 "tool_calls": [{"id": "call-9", "type": "function",
                                 "function": {"name": "read_file", "arguments": "{}"}}]},
                {"role": "tool", "tool_call_id": "call-9", "content": "file body"},
                {"role": "assistant", "content": "文件里是这些"}]
        events = [{"id": "call-9", "name": "read_file", "arguments": {},
                   "level": "L0", "decision": "allow_once", "ok": True,
                   "content": "file body", "durationMs": 12}]

        chat_persist.persist_chat_result(
            self.store, self.sid,
            loop_messages=loop, request_messages=request,
            system_prompt="你是助手", usage={"prompt_tokens": 10, "completion_tokens": 4},
            tool_events=events, model="MiniMax-M3", base_url=None)

        self.assertEqual(self._texts(), [("assistant", ""), ("tool", "file body"),
                                         ("assistant", "文件里是这些")])
        # No read API for tool_calls rows, so check the table directly.
        rows = self.store._conn.execute(
            "SELECT tool_name, permission_level, decision FROM tool_calls WHERE session_id = ?",
            (self.sid,)).fetchall()
        self.assertEqual([r[0] for r in rows], ["read_file"])
        self.assertEqual(rows[0][1], "L0")
        usage = self.store._conn.execute(
            "SELECT input_tokens, output_tokens FROM usage_log WHERE session_id = ?",
            (self.sid,)).fetchall()
        self.assertEqual([(u[0], u[1]) for u in usage], [(10, 4)])


if __name__ == "__main__":
    unittest.main()