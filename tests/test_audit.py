"""audit.py 单测：建表幂等、L0 空参数、三种裁决、规则增查、查询、重开持久。"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "python"))

from audit import SCHEMA_VERSION, AuditStore  # noqa: E402

EXPECTED_TABLES = {"schema_version", "tool_calls", "audit_log", "permission_rules"}


class AuditTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = os.path.join(self._tmp.name, "data.db")
        self.store = AuditStore(self.db)

    def tearDown(self):
        self.store.close()

    def _tables(self):
        conn = sqlite3.connect(self.db)
        try:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            return {r[0] for r in rows if not r[0].startswith("sqlite_")}
        finally:
            conn.close()

    def test_only_the_four_tables_are_created(self):
        self.assertEqual(self._tables(), EXPECTED_TABLES)

    def test_idempotent_construction_keeps_data(self):
        self.store.log_decision(tool_name="read_file", level="L0", decision="allow_once",
                                arguments={"path": "/x"})
        again = AuditStore(self.db)
        try:
            self.assertEqual(again.count_by_level().get("L0"), 1)
            self.assertEqual(self._tables(), EXPECTED_TABLES)
        finally:
            again.close()

    def test_schema_version_row(self):
        conn = sqlite3.connect(self.db)
        try:
            row = conn.execute("SELECT version FROM schema_version").fetchone()
            self.assertEqual(row[0], SCHEMA_VERSION)
        finally:
            conn.close()

    def test_wal_mode_enabled(self):
        conn = sqlite3.connect(self.db)
        try:
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            self.assertEqual(mode.lower(), "wal")
        finally:
            conn.close()

    def test_allowed_call_still_logs_arguments(self):
        self.store.log_decision(tool_name="read_file", level="L0", decision="allow_once",
                                arguments={"path": "/secret/a.txt"}, session_id="s1")
        rows = self.store.query_audit(session_id="s1")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["arguments"], {"path": "/secret/a.txt"})  # 放行也记参数
        self.assertFalse(rows[0]["reason"])          # 放行没有理由（也没有分级了）
        self.assertEqual(rows[0]["level"], "L0")
        self.assertEqual(rows[0]["tool_name"], "read_file")

    def test_three_decisions_persist_and_arguments_kept(self):
        args = {"command": "ls -la"}
        for decision in ("allow_once", "allow_always", "deny"):
            self.store.log_decision(tool_name="run_shell", level="L2", decision=decision,
                                    arguments=args, session_id="s2")
        rows = self.store.query_audit(session_id="s2")
        self.assertEqual({r["decision"] for r in rows},
                         {"allow_once", "allow_always", "deny"})
        for r in rows:
            self.assertEqual(r["arguments"], args)

    def test_invalid_decision_rejected(self):
        with self.assertRaises(ValueError):
            self.store.log_decision(tool_name="run_shell", level="L2", decision="maybe",
                                    arguments={})
        self.assertEqual(self.store.query_audit(), [])

    def test_query_by_level(self):
        self.store.log_decision(tool_name="read_file", level="L0", decision="allow_once",
                                arguments={})
        self.store.log_decision(tool_name="run_shell", level="L3",
                                decision="deny", arguments={"command": "rm -rf /"},
                                reason="user said no")
        self.assertEqual(len(self.store.query_audit(level="L3")), 1)
        self.assertEqual(len(self.store.query_audit(level="L0")), 1)
        self.assertEqual(sorted(self.store.count_by_level().items()), [("L0", 1), ("L3", 1)])

    def test_query_limit(self):
        for i in range(5):
            self.store.log_decision(tool_name="read_file", level="L0", decision="allow_once",
                                    arguments={})
        self.assertEqual(len(self.store.query_audit(limit=2)), 2)

    def test_tool_call_roundtrip(self):
        self.store.log_tool_call(
            id="tc-1", tool_name="write_file", arguments={"path": "/w/a.txt"},
            level="L1", decision="allow_once", session_id="s3", message_id="m1",
            result={"bytes_written": 3}, duration_ms=12,
            started_at=None, finished_at=None)
        conn = sqlite3.connect(self.db)
        try:
            row = conn.execute("SELECT * FROM tool_calls WHERE id='tc-1'").fetchone()
            self.assertIsNotNone(row)
            cols = [c[0] for c in conn.execute(
                "SELECT * FROM tool_calls LIMIT 1").description]
            item = dict(zip(cols, row))
        finally:
            conn.close()
        self.assertEqual(item["permission_level"], "L1")
        self.assertEqual(item["decision"], "allow_once")
        self.assertEqual(item["duration_ms"], 12)
        self.assertIn("bytes_written", item["result_json"])
        self.assertIsNone(item["error_json"])
        self.assertTrue(item["started_at"])       # 默认 datetime('now') 兜底

    def test_permission_rules_add_find_list(self):
        rid = self.store.add_permission_rule(rule_type="path", pattern="/w/sub",
                                             scope="workspace", level="L1", reason="user ok")
        self.assertIsInstance(rid, int)
        found = self.store.find_permission_rule(rule_type="path", pattern="/w/sub",
                                                scope="workspace")
        self.assertIsNotNone(found)
        self.assertEqual(found["level"], "L1")
        self.assertEqual(found["reason"], "user ok")
        self.assertTrue(found["created_at"])      # SQLite 侧时间戳
        self.store.add_permission_rule(rule_type="path", pattern="/other",
                                       scope="global", level="L1")
        self.assertEqual(len(self.store.list_permission_rules()), 2)
        self.assertEqual(len(self.store.list_permission_rules(scope="workspace")), 1)

    def test_find_missing_rule_returns_none(self):
        self.assertIsNone(self.store.find_permission_rule(rule_type="path", pattern="/nope",
                                                          scope="workspace"))

    def test_creates_parent_directory(self):
        nested = os.path.join(self._tmp.name, "deep", "dir", "data.db")
        s = AuditStore(nested)
        try:
            self.assertTrue(os.path.exists(nested))
        finally:
            s.close()

    def test_close_is_safe_twice(self):
        self.store.close()
        self.store.close()


class ReopenTest(unittest.TestCase):
    def test_data_survives_reopen(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "data.db")
            s = AuditStore(db)
            s.log_decision(tool_name="run_shell", level="L2", decision="allow_once",
                           arguments={"command": "ls"})
            s.add_permission_rule(rule_type="path", pattern="/w", scope="workspace", level="L1")
            s.close()

            again = AuditStore(db)
            try:
                self.assertEqual(len(again.query_audit()), 1)
                self.assertEqual(len(again.list_permission_rules()), 1)
            finally:
                again.close()


if __name__ == "__main__":
    unittest.main()