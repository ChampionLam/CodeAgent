"""SQLite 审计：裁决留痕 + 工具调用记录 + 已记住的权限规则。

本模块独占数据库文件（一份数据文件一个写入者）。表结构照旧设计文档抄，
本轮只建也只写四张表：schema_version / tool_calls / audit_log / permission_rules。
sessions / messages / usage_log 属于会话存储，不在本轮范围。

时间戳一律用 SQLite 的 datetime('now')，不用 Python 侧时间。
连接开 WAL + synchronous=NORMAL（兼顾安全与性能），check_same_thread=False
因为 sidecar 里 chat 跑在工作线程上。
"""
from __future__ import annotations

import json
import os
import sqlite3

SCHEMA_VERSION = 1

DECISIONS = ("allow_once", "allow_always", "deny")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (
  version INTEGER PRIMARY KEY,
  applied_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS tool_calls (
  id TEXT PRIMARY KEY,
  session_id TEXT,
  message_id TEXT,
  tool_name TEXT NOT NULL,
  arguments_json TEXT NOT NULL,
  result_json TEXT,
  error_json TEXT,
  permission_level TEXT NOT NULL,
  decision TEXT NOT NULL,
  duration_ms INTEGER,
  started_at TEXT NOT NULL DEFAULT (datetime('now')),
  finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_tool_calls_session ON tool_calls(session_id);

CREATE TABLE IF NOT EXISTS audit_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT,
  tool_call_id TEXT,
  tool_name TEXT NOT NULL,
  arguments_json TEXT NOT NULL,
  level TEXT NOT NULL,
  decision TEXT NOT NULL,
  reason TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_audit_log_created ON audit_log(created_at DESC);

CREATE TABLE IF NOT EXISTS permission_rules (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  rule_type TEXT NOT NULL,
  pattern TEXT NOT NULL,
  scope TEXT NOT NULL,
  level TEXT NOT NULL,
  reason TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_permission_rules_scope ON permission_rules(scope);
"""


def _dumps(value) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False,
                      separators=(",", ":"))


def _loads(value):
    if value is None:
        return None
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return None


class AuditStore:
    def __init__(self, db_path: str) -> None:
        self.db_path = os.path.abspath(os.path.expanduser(db_path))
        parent = os.path.dirname(self.db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(_SCHEMA)
        self._conn.execute(
            "INSERT OR IGNORE INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
        self._conn.commit()

    # -- 裁决留痕 -----------------------------------------------------------
    def log_decision(self, *, tool_name: str, level: str, decision: str, arguments: dict,
                     session_id: str | None = None, tool_call_id: str | None = None,
                     reason: str | None = None) -> int:
        if decision not in DECISIONS:
            raise ValueError("decision 必须是 %s 之一，收到 %r" % (DECISIONS, decision))
        # 2026-09-25 起 level 存 tier（hardline / dangerous / ""）。放行的调用
        # （tier=""）只记计数、不记详细参数；要审批的保留参数以便追责。
        if not level:
            arguments_json = "{}"
            reason = reason or "auto-allow"
        else:
            arguments_json = _dumps(arguments)
        cur = self._conn.execute(
            "INSERT INTO audit_log (session_id, tool_call_id, tool_name, arguments_json,"
            " level, decision, reason) VALUES (?,?,?,?,?,?,?)",
            (session_id, tool_call_id, tool_name, arguments_json, level, decision, reason))
        self._conn.commit()
        return int(cur.lastrowid)

    def log_tool_call(self, *, id: str, tool_name: str, arguments: dict, level: str,
                      decision: str, session_id: str | None = None,
                      message_id: str | None = None, result: dict | None = None,
                      error: dict | None = None, duration_ms: int | None = None,
                      started_at: str | None = None, finished_at: str | None = None) -> None:
        self._conn.execute(
            "INSERT INTO tool_calls (id, session_id, message_id, tool_name, arguments_json,"
            " result_json, error_json, permission_level, decision, duration_ms,"
            " started_at, finished_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?, COALESCE(?, datetime('now')), ?)",
            (id, session_id, message_id, tool_name, _dumps(arguments),
             None if result is None else _dumps(result),
             None if error is None else _dumps(error),
             level, decision, duration_ms, started_at, finished_at))
        self._conn.commit()

    # -- 权限规则 -----------------------------------------------------------
    def add_permission_rule(self, *, rule_type: str, pattern: str, scope: str, level: str,
                            reason: str | None = None) -> int:
        cur = self._conn.execute(
            "INSERT INTO permission_rules (rule_type, pattern, scope, level, reason)"
            " VALUES (?,?,?,?,?)", (rule_type, pattern, scope, level, reason))
        self._conn.commit()
        return int(cur.lastrowid)

    def find_permission_rule(self, *, rule_type: str, pattern: str, scope: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM permission_rules WHERE rule_type=? AND pattern=? AND scope=?"
            " ORDER BY id LIMIT 1", (rule_type, pattern, scope)).fetchone()
        return dict(row) if row else None

    def list_permission_rules(self, *, scope: str | None = None) -> list[dict]:
        if scope is None:
            rows = self._conn.execute("SELECT * FROM permission_rules ORDER BY id").fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM permission_rules WHERE scope=? ORDER BY id", (scope,)).fetchall()
        return [dict(r) for r in rows]

    # -- 查询 ---------------------------------------------------------------
    def query_audit(self, *, session_id: str | None = None, level: str | None = None,
                    limit: int = 200) -> list[dict]:
        sql = "SELECT * FROM audit_log WHERE 1=1"
        params: list = []
        if session_id is not None:
            sql += " AND session_id=?"
            params.append(session_id)
        if level is not None:
            sql += " AND level=?"
            params.append(level)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(int(limit))
        rows = self._conn.execute(sql, params).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["arguments"] = _loads(item.pop("arguments_json", None)) or {}
            out.append(item)
        return out

    def count_by_level(self) -> dict[str, int]:
        rows = self._conn.execute(
            "SELECT level, COUNT(*) AS n FROM audit_log GROUP BY level").fetchall()
        return {r["level"]: int(r["n"]) for r in rows}

    def close(self) -> None:
        try:
            self._conn.close()
        except sqlite3.Error:
            pass