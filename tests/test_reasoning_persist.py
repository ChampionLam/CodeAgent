"""思考通道落库测试：正文只留答案，思考单独进 reasoning 列。

2026-09-24 现场：assistant 的 content 里带着 `<thinking>…</thinking>` 原文。
分流器把思考发给了界面（当场看是对的），但落库和回灌模型用的是原始 delta ——
界面一重载，整段思考就当作正文显示出来。这个文件守三件事：

1. 落库正文不带标签（append_message 存的是什么就是什么，由 agent_loop 保证）；
2. 思考能单独存、单独读回（reasoning 列 + set_message_reasoning）；
3. 历史脏数据在启动时被搬进 reasoning（幂等，跑两遍结果一样）。
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "python"))

import chat_persist  # noqa: E402
import sessionstore  # noqa: E402

TAGGED = "<thinking>先看时间，再看天气</thinking>深圳今天晴，27 度。"


class ReasoningColumnTest(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.mkdtemp(prefix="desk-reasoning-")
        self.path = os.path.join(self.dir, "sessions.db")
        self.store = sessionstore.SessionStore(self.path)
        self.sid = self.store.create_session(model="MiniMax-M3", title="t")

    def tearDown(self) -> None:
        self.store.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_reasoning_round_trips(self) -> None:
        self.store.append_message(self.sid, {
            "role": "assistant", "content": "答案", "reasoning": "想了一下"})
        rows = self.store.get_messages(self.sid)
        self.assertEqual(rows[-1]["content"], "答案")
        self.assertEqual(rows[-1]["reasoning"], "想了一下")

    def test_set_message_reasoning_appends(self) -> None:
        row = self.store.append_message(self.sid, {"role": "assistant", "content": "答案"})
        self.assertIsNone(self.store.get_messages(self.sid)[-1]["reasoning"])
        self.store.set_message_reasoning(self.sid, row["id"], "第一段")
        self.store.set_message_reasoning(self.sid, row["id"], "第二段")
        self.assertEqual(self.store.get_messages(self.sid)[-1]["reasoning"], "第一段第二段")

    def test_set_message_reasoning_is_quiet_on_unknown_id(self) -> None:
        self.store.set_message_reasoning(self.sid, "msg_nope", "无所谓")
        self.assertEqual(self.store.get_messages(self.sid), [])

    def test_persist_attaches_reasoning_to_last_assistant(self) -> None:
        loop_messages = [
            {"role": "user", "content": "问题"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "call-1", "type": "function",
                 "function": {"name": "read_file", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "call-1", "content": "file body"},
            {"role": "assistant", "content": "答案"},
        ]
        chat_persist.persist_chat_result(
            self.store, self.sid,
            loop_messages=loop_messages,
            request_messages=[{"role": "user", "content": "问题"}],
            system_prompt=None, usage=None, tool_events=[],
            reasoning="思考了很久", model="MiniMax-M3", base_url=None)

        rows = self.store.get_messages(self.sid)
        self.assertEqual(rows[-1]["content"], "答案")
        self.assertEqual(rows[-1]["reasoning"], "思考了很久")
        # 思考只挂最后一条 assistant，中间的载体行不受影响
        carriers = [r for r in rows if r["role"] == "assistant" and not r["content"]]
        self.assertTrue(all(not r["reasoning"] for r in carriers))


class LegacyRepairTest(unittest.TestCase):
    """补历史：分流修好之前落库的行，正文里还夹着标签原文。"""

    def setUp(self) -> None:
        self.dir = tempfile.mkdtemp(prefix="desk-reasoning-legacy-")
        self.path = os.path.join(self.dir, "sessions.db")
        self.store = sessionstore.SessionStore(self.path)
        self.sid = self.store.create_session(model="MiniMax-M3", title="t")
        self.store.close()

    def tearDown(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)

    def _write_legacy_row(self, content: str) -> None:
        conn = sqlite3.connect(self.path)
        conn.execute(
            "INSERT INTO messages (id, session_id, role, content, ordinal, active)"
            " VALUES (?,?,?,?,?,1)",
            ("msg_legacy", self.sid, "assistant", content, 0))
        conn.commit()
        conn.close()

    def test_tags_move_out_of_content_on_open(self) -> None:
        self._write_legacy_row(TAGGED)
        store = sessionstore.SessionStore(self.path)   # 构造时跑一次修数据
        try:
            row = store.get_messages(self.sid)[-1]
            self.assertEqual(row["content"], "深圳今天晴，27 度。")
            self.assertEqual(row["reasoning"], "先看时间，再看天气")
        finally:
            store.close()

    def test_repair_is_idempotent(self) -> None:
        self._write_legacy_row(TAGGED)
        for _ in range(3):
            store = sessionstore.SessionStore(self.path)
            try:
                row = store.get_messages(self.sid)[-1]
                self.assertEqual(row["content"], "深圳今天晴，27 度。")
                self.assertEqual(row["reasoning"], "先看时间，再看天气")
            finally:
                store.close()

    def test_clean_rows_are_left_alone(self) -> None:
        self._write_legacy_row("什么都没有")
        store = sessionstore.SessionStore(self.path)
        try:
            row = store.get_messages(self.sid)[-1]
            self.assertEqual(row["content"], "什么都没有")
            self.assertIsNone(row["reasoning"])
        finally:
            store.close()

    def test_unclosed_tag_is_not_half_eaten(self) -> None:
        # 标签没闭合（流被截断）时不许猜：正文原样保留，别把用户的字吃掉
        self._write_legacy_row("<thinking>没闭合的思考，正文在这里")
        store = sessionstore.SessionStore(self.path)
        try:
            row = store.get_messages(self.sid)[-1]
            self.assertEqual(row["content"], "<thinking>没闭合的思考，正文在这里")
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main()