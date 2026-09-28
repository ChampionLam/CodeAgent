"""Session store tests: migrations, soft archive, round-trip, FTS trigram.

Design sources: concepts/desktop-agent-app-design.md (001_initial.sql) and
concepts/desktop-agent-context-mechanism-v1.md (004 + FTS5 trigram). The
FTS5 rows here are the acceptance check #6 of the context-mechanism design:
a Chinese substring query must return hits (the default tokenizer returns
zero for Chinese and never reports an error).
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "python"))

import sessionstore  # noqa: E402

PHRASE = "天气不错适合骑车"


class StoreCase(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.mkdtemp(prefix="desk-sessions-")
        self.path = os.path.join(self.dir, "sessions.db")
        self.store = sessionstore.SessionStore(self.path)

    def tearDown(self) -> None:
        self.store.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def _session(self, title="会话一", model="MiniMax-M3"):
        return self.store.create_session(model=model, title=title)


class MigrationTest(StoreCase):
    def test_schema_version_and_rerun_is_a_noop(self) -> None:
        # 007_project_sessions.sql 之后是 7（sessions 的项目会话字段）。
        # 版本号变化必须是有意的迁移，所以这里写死数字而不是取最大值。
        self.assertEqual(self.store.schema_version(), 7)
        # Reopening the same file must apply nothing and keep the same version.
        again = sessionstore.SessionStore(self.path)
        try:
            self.assertEqual(again.schema_version(), 7)
            self.assertEqual(again.list_sessions(), [])
        finally:
            again.close()


class SoftArchiveTest(StoreCase):
    def test_soft_delete_hides_from_list_but_keeps_messages(self) -> None:
        sid = self._session()
        self.store.append_message(sid, {"role": "user", "content": "第一句"})
        self.store.append_message(sid, {"role": "assistant", "content": "答一"})
        self.store.delete_session(sid)

        self.assertEqual([s["id"] for s in self.store.list_sessions()], [])
        self.assertEqual([s["id"] for s in self.store.list_sessions(include_deleted=True)], [sid])
        rows = self.store.get_messages(sid, include_inactive=True)
        self.assertEqual([r["content"] for r in rows], ["第一句", "答一"])

    def test_deactivate_messages_is_non_destructive(self) -> None:
        sid = self._session()
        self.store.append_messages(sid, [
            {"role": "user", "content": "旧的"},
            {"role": "assistant", "content": "旧的回答"},
            {"role": "user", "content": "新的"},
        ])
        n = self.store.deactivate_messages(sid, from_ordinal=0, to_ordinal=1)
        self.assertEqual(n, 2)
        self.assertEqual([r["content"] for r in self.store.get_messages(sid)], ["新的"])
        self.assertEqual(len(self.store.get_messages(sid, include_inactive=True)), 3)


class RoundTripTest(StoreCase):
    def test_ordinals_increase_and_tool_payloads_survive(self) -> None:
        sid = self._session()
        calls = [{"id": "call_1", "type": "function",
                  "function": {"name": "read_file", "arguments": '{"path":"a.txt"}'}}]
        first = self.store.append_message(sid, {"role": "user", "content": "读一下"})
        second = self.store.append_message(
            sid, {"role": "assistant", "content": None, "tool_calls": calls})
        third = self.store.append_message(
            sid, {"role": "tool", "content": "文件内容", "tool_call_id": "call_1"})
        self.assertEqual([first["ordinal"], second["ordinal"], third["ordinal"]], [0, 1, 2])

        rows = self.store.get_messages(sid)
        self.assertEqual(rows[1]["toolCalls"], calls)
        self.assertEqual(rows[2]["toolCallId"], "call_1")

    def test_usage_totals_accumulate_on_the_session(self) -> None:
        sid = self._session()
        for i in range(2):
            self.store.append_usage(session_id=sid, model="MiniMax-M3",
                                    input_tokens=100, output_tokens=20,
                                    cached_input_tokens=50, cost_usd=0.0)
        row = self.store.get_session(sid)
        self.assertEqual(row["total_input_tokens"], 200)
        self.assertEqual(row["total_output_tokens"], 40)

    def test_list_orders_by_updated_at_desc(self) -> None:
        first = self._session(title="先建的")
        time.sleep(1.1)
        second = self._session(title="后建的")
        time.sleep(1.1)
        self.store.append_message(first, {"role": "user", "content": "把它顶上去"})
        ids = [s["id"] for s in self.store.list_sessions()]
        self.assertEqual(ids, [first, second])


class FtsTrigramTest(StoreCase):
    """Acceptance check 6: Chinese substring search must return hits."""

    def test_chinese_substring_hits_and_sessions_do_not_bleed(self) -> None:
        a = self._session(title="会话甲")
        b = self._session(title="会话乙")
        self.store.append_message(a, {"role": "user", "content": "今天" + PHRASE})
        self.store.append_message(b, {"role": "assistant", "content": "他问" + PHRASE + "吗"})

        hits = self.store.search_messages(PHRASE)
        self.assertEqual(len(hits), 2, hits)
        self.assertEqual({h["sessionId"] for h in hits}, {a, b})
        # Same ordinal in two sessions must not overwrite each other's index.
        self.assertEqual({h["ordinal"] for h in hits}, {0})

        scoped = self.store.search_messages(PHRASE, session_id=b)
        self.assertEqual([h["sessionId"] for h in scoped], [b])
        self.assertTrue(scoped[0]["snippet"])

    def test_search_ignores_messages_without_content(self) -> None:
        sid = self._session()
        self.store.append_message(sid, {"role": "assistant", "content": None,
                                        "tool_calls": [{"id": "c", "type": "function",
                                                        "function": {"name": "f", "arguments": "{}"}}]})
        self.assertEqual(self.store.search_messages(PHRASE), [])


if __name__ == "__main__":
    unittest.main()