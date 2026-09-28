"""Session persistence for the desk-agent sidecar (sessions.db).

Style follows audit.py (one connection per store, WAL, check_same_thread=False,
own lock because chat runs on a worker thread). Differences from audit.py:

  * Schema comes from SQL migration files (python/migrations/NNN_*.sql), not a
    hand-written executescript block, so the DDL stays reviewable and matches
    the design doc verbatim. The runner is idempotent: it applies only
    migrations whose version is missing from schema_version, each inside one
    transaction, rolling back on failure.
  * Migrations can contain CREATE TRIGGER bodies, so statements are split with
    sqlite3.complete_statement (executescript would also commit implicitly and
    break the "all in one transaction" requirement).

Soft archive discipline (context mechanism v1): delete_session only flips
status='deleted'; rows are never physically removed, and get_messages
include_inactive=True always returns the full original text.

FTS5 trigram index: messages_fts is an external-content virtual table whose
content table is messages_fts_trigram_src. That mapping table turns
(session_id, ordinal) into a globally unique integer src_id -- feeding ordinal
straight in would let two sessions overwrite each other's index rows. The
triggers in 004 keep both in sync.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
import uuid

MIGRATIONS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "migrations")

TITLE_MAX_CHARS = 30

STATUS_ACTIVE = "active"
STATUS_DELETED = "deleted"

# 007: 会话类型。项目会话本质还是 session，只是带额外属性。
KIND_CHAT = "chat"
KIND_PROJECT = "project"
SESSION_KINDS = (KIND_CHAT, KIND_PROJECT)


class SessionValidationError(ValueError):
    """项目会话字段没过校验（目录不存在 / 名字为空 / kind 非法）。

    和 LookupError 分开报：RPC 层要把前者翻成 BAD_REQUEST、后者翻成
    SESSION_NOT_FOUND，混在一个异常里界面就只能拿到 INTERNAL。
    """

    def __init__(self, message: str, code: str = "BAD_REQUEST") -> None:
        super().__init__(message)
        self.code = code

# Session-level model defaults used when nothing better is known.
DEFAULT_MODEL = "default"
DEFAULT_BASE_URL: str | None = None


def _clean_session_kind(kind: str | None) -> str:
    """kind 只认 chat/project；空串按默认 chat 处理（None 同）。"""
    value = (kind or "").strip() if isinstance(kind, str) else (kind or "")
    if value in (None, ""):
        return KIND_CHAT
    if value not in SESSION_KINDS:
        raise SessionValidationError(
            "kind must be one of %s, got %r" % (", ".join(SESSION_KINDS), kind))
    return value


def _clean_project_name(name: str | None) -> str | None:
    """去首尾空白；传了但只剩空白 -> 报错（不允许「空名项目」）。"""
    if name is None:
        return None
    if not isinstance(name, str):
        raise SessionValidationError("projectName must be a string")
    stripped = name.strip()
    if not stripped:
        raise SessionValidationError("projectName must not be blank")
    return stripped


def _clean_workspace_root(root: str | None) -> str | None:
    """非空时必须真实存在且是目录，展开 ~ 后按绝对路径存。"""
    if root is None:
        return None
    if not isinstance(root, str):
        raise SessionValidationError("workspaceRoot must be a string")
    stripped = root.strip()
    if not stripped:
        return None
    expanded = os.path.abspath(os.path.expanduser(stripped))
    if not os.path.isdir(expanded):
        raise SessionValidationError(
            "workspaceRoot is not an existing directory: %s" % expanded)
    return expanded


def _clean_json_list(value) -> str | None:
    """project_skills / project_files 的入库口径。

    list（含空 list）-> 紧凑 JSON；None -> NULL（不写）。字符串原样入库
    （调用方自己拼的 JSON 也认，坏了读回时当空）。
    """
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        return json.dumps(list(value), ensure_ascii=False, separators=(",", ":"))
    if isinstance(value, str):
        return value
    raise SessionValidationError(
        "expected a JSON list or string, got %s" % type(value).__name__)


def _session_project_fields(row) -> dict:
    """sessions 行 -> 007 五个字段的读回形状（JSON 坏数据当空）。"""
    if row is None:
        return {}
    keys = row.keys() if hasattr(row, "keys") else []
    out: dict = {}
    if "kind" in keys:
        out["kind"] = (row["kind"] or KIND_CHAT)
    if "project_name" in keys:
        out["project_name"] = row["project_name"]
    if "workspace_root" in keys:
        out["workspace_root"] = row["workspace_root"]
    if "project_skills" in keys:
        out["project_skills"] = _loads(row["project_skills"])
        if out["project_skills"] is None and (row["project_skills"] or "").strip():
            out["project_skills"] = []       # 坏 JSON 当空列表，不抛
    if "project_files" in keys:
        out["project_files"] = _loads(row["project_files"])
        if out["project_files"] is None and (row["project_files"] or "").strip():
            out["project_files"] = []        # 坏 JSON 当空列表，不抛
    return out


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


def _split_statements(sql: str) -> list[str]:
    """Split a migration file into whole SQL statements.

    A simple line/semicolon split cannot handle CREATE TRIGGER bodies (they
    contain semicolons inside BEGIN..END). sqlite3.complete_statement() is
    parser-aware, so each emitted chunk is a complete statement.
    """
    statements: list[str] = []
    buf = ""
    for line in sql.splitlines(keepends=True):
        buf += line
        if sqlite3.complete_statement(buf):
            statements.append(buf)
            buf = ""
    leftover = buf.strip()
    if leftover and not leftover.startswith("--"):
        raise ValueError("migration ends mid-statement: %r" % leftover[:80])
    return statements


def _discover_migrations() -> list[tuple[int, str]]:
    """List migration files as sorted (version, path) pairs."""
    out: list[tuple[int, str]] = []
    if not os.path.isdir(MIGRATIONS_DIR):
        return out
    for name in os.listdir(MIGRATIONS_DIR):
        if not name.endswith(".sql"):
            continue
        stem = name[:-4]
        if not stem[:3].isdigit() or "_" not in stem:
            continue
        try:
            version = int(stem.split("_", 1)[0])
        except ValueError:
            continue
        out.append((version, os.path.join(MIGRATIONS_DIR, name)))
    return sorted(out, key=lambda pair: pair[0])


def run_migrations(conn: sqlite3.Connection) -> list[int]:
    """Apply all pending migrations, idempotently.

    Each migration runs inside one transaction; on failure the transaction is
    rolled back and the error propagates. Already-applied versions are
    skipped, so calling this repeatedly is safe.
    """
    applied: list[int] = []
    versions = {int(r[0]) for r in
                conn.execute("SELECT version FROM schema_version").fetchall()} \
        if _has_table(conn, "schema_version") else set()
    for version, path in _discover_migrations():
        if version in versions:
            continue
        with open(path, encoding="utf-8") as fh:
            sql = fh.read()
        statements = _split_statements(sql)
        conn.execute("BEGIN IMMEDIATE")
        try:
            for stmt in statements:
                conn.execute(stmt)
            conn.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        applied.append(version)
    return applied


# 模型把思考塞进正文时用的标签（reasoning_split.TAGS 的同款集合）。
_THINK_TAG_RE = re.compile(
    r"<(thinking|think|reasoning|thought|REASONING_SCRATCHPAD)>(.*?)</\1>",
    re.IGNORECASE | re.DOTALL)


def _repair_thinking_in_content(conn: sqlite3.Connection) -> int:
    """把历史消息里没分流的 <thinking> 块搬进 reasoning 列（幂等，2026-09-24）。

    这条是补历史的：分流修好之前落库的 assistant 正文里带着标签原文，
    界面重载后整段思考就露在正文里。只碰带标签的行，改完 content 只剩答案、
    reasoning 存思考；跑过一遍后再跑就查不到带标签的行了。
    """
    try:
        rows = conn.execute(
            "SELECT id, content FROM messages"
            " WHERE content LIKE '%<think%' OR content LIKE '%<reasoning%'"
            " OR content LIKE '%<thought%'").fetchall()
    except sqlite3.Error:
        return 0
    fixed = 0
    for row in rows:
        text = row["content"] or ""
        found = [m.group(2).strip() for m in _THINK_TAG_RE.finditer(text)]
        if not found:
            continue
        cleaned = _THINK_TAG_RE.sub("", text).strip()
        try:
            conn.execute("UPDATE messages SET content=?, reasoning=? WHERE id=?",
                         (cleaned, "\n\n".join(found), row["id"]))
            fixed += 1
        except sqlite3.Error:
            continue
    if fixed:
        conn.commit()
    return fixed


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()
    return row is not None


def _quote_phrase(term: str) -> str:
    """Escape one search term as an FTS5 quoted string.

    Doubles embedded double quotes; the result is wrapped in FTS5 string
    quotes so user text (CJK, punctuation, operators) is matched literally
    instead of being parsed as FTS5 syntax.
    """
    return '"' + term.replace('"', '""') + '"'


class SessionStore:
    """Owns sessions.db. One connection, one lock, soft deletes only."""

    def __init__(self, db_path: str) -> None:
        self.db_path = os.path.abspath(os.path.expanduser(db_path))
        parent = os.path.dirname(self.db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.isolation_level = None  # explicit BEGIN/COMMIT
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        run_migrations(self._conn)
        _repair_thinking_in_content(self._conn)

    # ------------------------------------------------------------------ basics
    def close(self) -> None:
        try:
            with self._lock:
                self._conn.close()
        except sqlite3.Error:
            pass

    def schema_version(self) -> int | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT MAX(version) FROM schema_version").fetchone()
        return None if row is None or row[0] is None else int(row[0])

    # ---------------------------------------------------------------- sessions
    def create_session(self, *, model: str = DEFAULT_MODEL,
                       base_url: str | None = DEFAULT_BASE_URL,
                       title: str = "", session_id: str | None = None,
                       context_length: int | None = None,
                       kind: str | None = None,
                       project_name: str | None = None,
                       workspace_root: str | None = None,
                       project_skills: list | str | None = None,
                       project_files: list | str | None = None) -> str:
        # The design doc's 004 migration adds context_length as NOT NULL with
        # no DEFAULT, so SQLite forces us to always supply it. 0 means "window
        # not yet persisted"; None is not allowed at the SQL layer.
        #
        # 007: project sessions carry five extra columns. Validation happens
        # BEFORE any write so a bad request never leaves a half-row behind.
        kind_v = _clean_session_kind(kind)
        name_v = _clean_project_name(project_name)
        ws_v = _clean_workspace_root(workspace_root)
        skills_v = _clean_json_list(project_skills)
        files_v = _clean_json_list(project_files)
        sid = session_id or uuid.uuid4().hex
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute(
                    "INSERT INTO sessions (id, title, model, base_url, context_length,"
                    " kind, project_name, workspace_root, project_skills, project_files)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (sid, title, model, base_url,
                     0 if context_length is None else int(context_length),
                     kind_v, name_v, ws_v, skills_v, files_v))
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        return sid

    def update_project(self, session_id: str, *,
                       project_name: str | None = None,
                       workspace_root: str | None = None,
                       project_skills: list | str | None = None,
                       project_files: list | str | None = None) -> None:
        """007: 只改传了的字段；未知 session_id 报 LookupError 不静默。

        注意区分「没传」（None，保持原值）和「显式清空」：name/root 传空串
        表示清掉该字段；skills/files 传空 list 表示清空清单。校验口径与
        create_session 一致（目录必须存在、名字去空白后非空）。
        """
        # 校验在事务外面做：坏参数不动库。
        name_given = project_name is not None
        root_given = workspace_root is not None
        skills_given = project_skills is not None
        files_given = project_files is not None
        name_v = _clean_project_name(project_name) if name_given else None
        root_v = _clean_workspace_root(workspace_root) if root_given else None
        skills_v = _clean_json_list(project_skills) if skills_given else None
        files_v = _clean_json_list(project_files) if files_given else None
        sets: list[str] = []
        args: list = []
        if name_given:
            sets.append("project_name = ?")
            args.append(name_v)
        if root_given:
            sets.append("workspace_root = ?")
            args.append(root_v)
        if skills_given:
            sets.append("project_skills = ?")
            args.append(skills_v)
        if files_given:
            sets.append("project_files = ?")
            args.append(files_v)
        if not sets:
            # 全没传：仍然要求 session 存在（调用方要知道 id 写错了）。
            with self._lock:
                if not _has_table_row(self._conn, "sessions", session_id):
                    raise LookupError("no such session: %s" % session_id)
            return
        sets.append("updated_at = datetime('now')")
        args.append(session_id)
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                cur = self._conn.execute(
                    "UPDATE sessions SET %s WHERE id=?" % ", ".join(sets),
                    tuple(args))
                if cur.rowcount != 1:
                    raise LookupError("no such session: %s" % session_id)
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def rename_session(self, session_id: str, title: str) -> None:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                cur = self._conn.execute(
                    "UPDATE sessions SET title=?, updated_at=datetime('now')"
                    " WHERE id=?", (title, session_id))
                if cur.rowcount != 1:
                    raise LookupError("no such session: %s" % session_id)
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def delete_session(self, session_id: str) -> None:
        """Soft delete: status='deleted'. Rows are never physically removed."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                cur = self._conn.execute(
                    "UPDATE sessions SET status=?, updated_at=datetime('now')"
                    " WHERE id=?", (STATUS_DELETED, session_id))
                if cur.rowcount != 1:
                    raise LookupError("no such session: %s" % session_id)
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def append_compaction_record(self, *, session_id: str, generation: int, trigger: str,
                                 tokens_before: int, tokens_after: int,
                                 summary_text: str = "",
                                 range_start_ordinal: int | None = None,
                                 range_end_ordinal: int | None = None) -> None:
        """Audit one compaction.

        (session_id, generation) is unique, so replaying the same generation
        updates the row instead of duplicating it. L0/L1-only compactions do
        not bump the generation and are reported through the session state
        instead of a record row.
        """
        self._conn.execute(
            "INSERT INTO compaction_records (session_id, generation, trigger,"
            " range_start_ordinal, range_end_ordinal, tokens_before, tokens_after, summary_text)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(session_id, generation) DO UPDATE SET"
            " trigger = excluded.trigger, tokens_before = excluded.tokens_before,"
            " tokens_after = excluded.tokens_after, summary_text = excluded.summary_text",
            (session_id, int(generation), str(trigger), range_start_ordinal,
             range_end_ordinal, int(tokens_before), int(tokens_after), str(summary_text)))

    def append_spilled_output(self, *, session_id: str, path: str, content: str,
                              preview_head: str, preview_tail: str,
                              tool_call_id: str | None = None) -> None:
        """Index one spilled L1 output.

        byte count and digest are computed from the content that was written,
        so the row always describes the file that actually reached the disk.
        """
        blob = content.encode("utf-8")
        self._conn.execute(
            "INSERT INTO spilled_outputs (session_id, tool_call_id, path, bytes, sha256,"
            " preview_head, preview_tail) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (session_id, tool_call_id, path, len(blob),
             hashlib.sha256(blob).hexdigest(), preview_head, preview_tail))

    def set_context_state(self, session_id: str, *, context_length: int | None = None,
                          overflow_attempts: int | None = None) -> None:
        """Persist the context window facts for a session.

        Kept on the session row so a restarted app can show the same window and
        retry accounting without replaying the conversation.
        """
        sets: list[str] = []
        args: list[int] = []
        if context_length is not None:
            sets.append("context_length = ?")
            args.append(int(context_length))
        if overflow_attempts is not None:
            sets.append("overflow_attempts = ?")
            args.append(int(overflow_attempts))
        if not sets:
            return
        args.append(session_id)
        self._conn.execute(
            "UPDATE sessions SET %s WHERE id = ?" % ", ".join(sets), tuple(args))

    def get_session(self, session_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
        if row is None:
            return None
        out = dict(row)
        # 007: JSON 列读出为 list（坏数据当空），kind 缺省补 'chat'。
        out.update(_session_project_fields(row))
        return out

    def list_sessions(self, *, include_deleted: bool = False,
                      limit: int = 200) -> list[dict]:
        """Sessions ordered by updated_at DESC, with message count and tokens."""
        where = "" if include_deleted else " WHERE s.status != 'deleted'"
        sql = (
            "SELECT s.id, s.title, s.created_at, s.updated_at, s.model, s.base_url,"
            " s.status, s.total_input_tokens, s.total_output_tokens, s.total_cost_usd,"
            " s.context_length, s.overflow_attempts,"
            " s.kind, s.project_name, s.workspace_root, s.project_skills, s.project_files,"
            " (SELECT COUNT(*) FROM messages m WHERE m.session_id = s.id) AS message_count"
            " FROM sessions s%s ORDER BY s.updated_at DESC, s.id DESC LIMIT ?"
        ) % where
        with self._lock:
            rows = self._conn.execute(sql, (int(limit),)).fetchall()
        out = []
        for r in rows:
            item = dict(r)
            item["message_count"] = int(item.get("message_count") or 0)
            # 007: 同 get_session 的读回口径（JSON -> list，坏数据当空）。
            item.update(_session_project_fields(r))
            out.append(item)
        return out

    # ------------------------------------------------------------------
    # usage aggregation —— 底部信息栏和用量统计页共用这一份数
    # ------------------------------------------------------------------
    #
    # 口径说明（用户明确要求「数据要对齐」）：
    #   * token 数只认 usage_log（每轮 chat.done 落一行，来自 provider 的
    #     usage 上报）。界面不许再按「字符数 ÷ 3.2」之类估算，否则底部栏和
    #     用量页会对不上。
    #   * 日期一律按本地时区切（date(request_at, 'localtime')），今天 =
    #     本地今天，窗口 = 含今天往回数 days 天。
    #   * 缓存命中 token 单列，不重复计入 input（provider 的 prompt_tokens
    #     含缓存命中部分，这里只做展示区分）。

    def _usage_sum(self, where: str, params: tuple) -> dict:
        row = self._conn.execute(
            "SELECT COUNT(*) AS calls,"
            " COALESCE(SUM(input_tokens), 0) AS input,"
            " COALESCE(SUM(output_tokens), 0) AS output,"
            " COALESCE(SUM(cached_input_tokens), 0) AS cached"
            " FROM usage_log" + where, params).fetchone()
        out = {
            "calls": int(row["calls"] or 0),
            "input": int(row["input"] or 0),
            "output": int(row["output"] or 0),
            "cached": int(row["cached"] or 0),
        }
        out["tokens"] = out["input"] + out["output"]
        return out

    def usage_summary(self, *, days: int = 7,
                      session_id: str | None = None) -> dict:
        """聚合用量。days 只影响 window / byDay / byModel / bySession 的窗口。"""
        days = max(1, int(days))
        since = "date('now', 'localtime', '-%d days')" % (days - 1)
        window_where = " WHERE date(request_at, 'localtime') >= " + since
        with self._lock:
            summary = {}
            summary["total"] = self._usage_sum("", ())
            summary["today"] = self._usage_sum(
                " WHERE date(request_at, 'localtime') = date('now', 'localtime')", ())
            # 今日按模型拆一份：底部栏要显示今日用量，不能只有总额。
            rows = self._conn.execute(
                "SELECT model, COUNT(*) AS calls, COALESCE(SUM(input_tokens),0) AS input,"
                " COALESCE(SUM(output_tokens),0) AS output,"
                " COALESCE(SUM(cached_input_tokens),0) AS cached"
                " FROM usage_log WHERE date(request_at,'localtime') = date('now','localtime')"
                " GROUP BY model ORDER BY (SUM(input_tokens)+SUM(output_tokens)) DESC").fetchall()
            summary["today"]["byModel"] = [{
                "model": r["model"], "calls": int(r["calls"]),
                "input": int(r["input"]), "output": int(r["output"]),
                "cached": int(r["cached"]),
                "tokens": int(r["input"]) + int(r["output"]),
            } for r in rows]
            summary["window"] = self._usage_sum(window_where, ())
            summary["windowDays"] = days
            rows = self._conn.execute(
                "SELECT date(request_at, 'localtime') AS day,"
                " COUNT(*) AS calls, COALESCE(SUM(input_tokens),0) AS input,"
                " COALESCE(SUM(output_tokens),0) AS output,"
                " COALESCE(SUM(cached_input_tokens),0) AS cached"
                " FROM usage_log" + window_where +
                " GROUP BY day ORDER BY day ASC").fetchall()
            by_day = [{
                "day": r["day"], "calls": int(r["calls"]),
                "input": int(r["input"]), "output": int(r["output"]),
                "cached": int(r["cached"]), "tokens": int(r["input"]) + int(r["output"]),
            } for r in rows]
            rows = self._conn.execute(
                "SELECT model, COUNT(*) AS calls, COALESCE(SUM(input_tokens),0) AS input,"
                " COALESCE(SUM(output_tokens),0) AS output,"
                " COALESCE(SUM(cached_input_tokens),0) AS cached"
                " FROM usage_log" + window_where +
                " GROUP BY model ORDER BY (SUM(input_tokens)+SUM(output_tokens)) DESC").fetchall()
            by_model = [{
                "model": r["model"], "calls": int(r["calls"]),
                "input": int(r["input"]), "output": int(r["output"]),
                "cached": int(r["cached"]), "tokens": int(r["input"]) + int(r["output"]),
            } for r in rows]
            rows = self._conn.execute(
                "SELECT u.session_id AS session_id, s.title AS title,"
                " COUNT(*) AS calls, COALESCE(SUM(u.input_tokens),0) AS input,"
                " COALESCE(SUM(u.output_tokens),0) AS output,"
                " COALESCE(SUM(u.cached_input_tokens),0) AS cached,"
                " MAX(u.request_at) AS last_at"
                " FROM usage_log u LEFT JOIN sessions s ON s.id = u.session_id" +
                window_where.replace("date(request_at", "date(u.request_at") +
                " GROUP BY u.session_id ORDER BY (SUM(u.input_tokens)+SUM(u.output_tokens)) DESC"
                " LIMIT 50").fetchall()
            by_session = [{
                "sessionId": r["session_id"], "title": r["title"],
                "calls": int(r["calls"]), "input": int(r["input"]),
                "output": int(r["output"]), "cached": int(r["cached"]),
                "tokens": int(r["input"]) + int(r["output"]),
                "lastAt": r["last_at"],
            } for r in rows]
            if session_id:
                summary["session"] = self._usage_sum(
                    " WHERE session_id = ?", (session_id,))
                rows = self._conn.execute(
                    "SELECT model, COUNT(*) AS calls,"
                    " COALESCE(SUM(input_tokens),0) AS input,"
                    " COALESCE(SUM(output_tokens),0) AS output,"
                    " COALESCE(SUM(cached_input_tokens),0) AS cached"
                    " FROM usage_log WHERE session_id = ? GROUP BY model"
                    " ORDER BY (SUM(input_tokens)+SUM(output_tokens)) DESC",
                    (session_id,)).fetchall()
                summary["session"]["byModel"] = [{
                    "model": r["model"], "calls": int(r["calls"]),
                    "input": int(r["input"]), "output": int(r["output"]),
                    "cached": int(r["cached"]),
                    "tokens": int(r["input"]) + int(r["output"]),
                } for r in rows]
                last = self._conn.execute(
                    "SELECT input_tokens, output_tokens, cached_input_tokens, model,"
                    " request_at FROM usage_log WHERE session_id = ?"
                    " ORDER BY request_at DESC, id DESC LIMIT 1", (session_id,)).fetchone()
                summary["session"]["lastTurn"] = ({
                    "input": int(last["input_tokens"]),
                    "output": int(last["output_tokens"]),
                    "cached": int(last["cached_input_tokens"]),
                    "model": last["model"], "at": last["request_at"],
                } if last else None)
        summary["byDay"] = by_day
        summary["byModel"] = by_model
        summary["bySession"] = by_session
        return summary

    def _touch_session(self, session_id: str) -> None:
        self._conn.execute(
            "UPDATE sessions SET updated_at=datetime('now') WHERE id=?", (session_id,))

    # ---------------------------------------------------------------- messages
    def append_message(self, session_id: str, message: dict) -> dict:
        """Append one message row; auto-assigns the next per-session ordinal.

        Stores assistant tool_calls under tool_calls_json (list) and tool-role
        tool_call_id, so a message round-trips byte for byte. Message ids are
        generated when absent. Also refreshes the session's updated_at.
        """
        role = message.get("role") or ""
        content = message.get("content")
        if isinstance(content, (dict, list)):
            content = json.dumps(content, ensure_ascii=False)
        tool_call_id = message.get("tool_call_id")
        tool_calls = message.get("tool_calls")
        if tool_calls is not None and not isinstance(tool_calls, str):
            tool_calls = json.dumps(tool_calls, ensure_ascii=False)
        message_id = message.get("id") or ("msg_" + uuid.uuid4().hex)

        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                if not _has_table_row(self._conn, "sessions", session_id):
                    raise LookupError("no such session: %s" % session_id)
                row = self._conn.execute(
                    "SELECT COALESCE(MAX(ordinal), -1) + 1 FROM messages"
                    " WHERE session_id=?", (session_id,)).fetchone()
                ordinal = int(row[0])
                self._conn.execute(
                    "INSERT INTO messages (id, session_id, role, content,"
                    " tool_call_id, tool_calls_json, ordinal, reasoning)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    (message_id, session_id, role, content,
                     tool_call_id, tool_calls, ordinal,
                     message.get("reasoning")))
                self._touch_session(session_id)
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        return {"id": message_id, "sessionId": session_id, "ordinal": ordinal}

    def set_message_artifacts(self, session_id: str, message_id: str,
                              payload: str) -> None:
        """把本轮的产出文件挂到某条已落库的 assistant 消息上（2026-09-27）。

        和 reasoning 一样：整轮结束才回填，失败不抛（附件丢了不影响正文）。
        """
        if not payload:
            return
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute(
                    "UPDATE messages SET artifacts=? WHERE id=? AND session_id=?",
                    (payload, message_id, session_id))
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def set_message_reasoning(self, session_id: str, message_id: str,
                              text: str) -> None:
        """给某条已落库的消息补思考正文（thinking 列，2026-09-24）。

        思考是一轮里多段拼起来的，落库时机在整轮结束之后，所以先 append 再回填。
        失败不抛：思考丢了不影响正文，但正文丢了才要命。
        """
        if not text:
            return
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute(
                    "UPDATE messages SET reasoning=COALESCE(reasoning, '') || ?"
                    " WHERE id=? AND session_id=?",
                    (text, message_id, session_id))
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")

    def append_messages(self, session_id: str, messages: list[dict]) -> list[dict]:
        return [self.append_message(session_id, m) for m in messages]

    def get_messages(self, session_id: str, *, include_inactive: bool = False,
                     limit: int = 5000) -> list[dict]:
        """Read a session's messages in ordinal order.

        Default view: active=1 AND compacted=0 (the context-mechanism live
        view). include_inactive=True returns every row, original text intact.
        """
        where = " AND active=1 AND compacted=0" if not include_inactive else ""
        sql = ("SELECT id, session_id, role, content, tool_call_id, tool_calls_json,"
               " created_at, ordinal, active, compacted, reasoning, artifacts"
               " FROM messages WHERE session_id=?%s"
               " ORDER BY ordinal" % where)
        with self._lock:
            rows = self._conn.execute(sql, (session_id,)).fetchall()
        out = []
        for r in rows:
            out.append(self._row_to_message(r))
        if len(out) > int(limit):
            out = out[: int(limit)]
        return out

    @staticmethod
    def _row_to_message(row) -> dict:
        msg = {
            "id": row["id"],
            "sessionId": row["session_id"],
            "role": row["role"],
            "content": row["content"],
            "reasoning": row["reasoning"],
            "artifacts": _loads(row["artifacts"]) if "artifacts" in row.keys() else [],
            "toolCallId": row["tool_call_id"],
            "toolCalls": _loads(row["tool_calls_json"]),
            "toolCallsJson": row["tool_calls_json"],
            "createdAt": row["created_at"],
            "ordinal": int(row["ordinal"]),
            "active": int(row["active"] or 0),
            "compacted": int(row["compacted"] or 0),
        }
        return msg

    def deactivate_messages(self, session_id: str, *,
                            from_ordinal: int, to_ordinal: int,
                            compacted: bool = True) -> int:
        """Flip active/compacted flags on an ordinal range (soft archive)."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                cur = self._conn.execute(
                    "UPDATE messages SET active=0, compacted=?"
                    " WHERE session_id=? AND ordinal>=? AND ordinal<=?",
                    (1 if compacted else 0, session_id, int(from_ordinal), int(to_ordinal)))
                self._conn.execute("COMMIT")
                return int(cur.rowcount)
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    # ---------------------------------------------------------------- tool calls
    def get_tool_calls(self, session_id: str) -> list[dict]:
        """Read a session's tool call rows (状态/结果/耗时), insertion order.

        2026-09-25: 这张表一直只有写没有读 —— 跨会话历史里的工具卡因此永远是
        空的（模型当时的工具调用没地方取回来）。只读，不改任何数据。
        """
        sql = ("SELECT id, session_id, message_id, tool_name, arguments_json,"
               " result_json, error_json, permission_level, decision, duration_ms,"
               " started_at, finished_at FROM tool_calls"
               " WHERE session_id=? ORDER BY rowid")
        with self._lock:
            rows = self._conn.execute(sql, (session_id,)).fetchall()
        return [{
            "id": r["id"],
            "sessionId": r["session_id"],
            "messageId": r["message_id"],
            "toolName": r["tool_name"],
            "arguments": _loads(r["arguments_json"]),
            "result": _loads(r["result_json"]),
            "error": _loads(r["error_json"]),
            "permissionLevel": r["permission_level"],
            "decision": r["decision"],
            "durationMs": r["duration_ms"],
            "startedAt": r["started_at"],
            "finishedAt": r["finished_at"],
        } for r in rows]

    def append_tool_call(self, *, id: str, session_id: str, message_id: str,
                         tool_name: str, arguments: dict | str,
                         permission_level: str, decision: str,
                         result: dict | str | None = None,
                         error: dict | str | None = None,
                         duration_ms: int | None = None,
                         started_at: str | None = None,
                         finished_at: str | None = None) -> None:
        args_json = arguments if isinstance(arguments, str) else _dumps(arguments)
        result_json = result if isinstance(result, str) else (
            None if result is None else _dumps(result))
        error_json = error if isinstance(error, str) else (
            None if error is None else _dumps(error))
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute(
                    "INSERT INTO tool_calls (id, session_id, message_id, tool_name,"
                    " arguments_json, result_json, error_json, permission_level,"
                    " decision, duration_ms, started_at, finished_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,"
                    " COALESCE(?, datetime('now')), ?)",
                    (id, session_id, message_id, tool_name, args_json, result_json,
                     error_json, permission_level, decision, duration_ms,
                     started_at, finished_at))
                self._touch_session(session_id)
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    # ---------------------------------------------------------------- usage log
    def append_usage(self, *, session_id: str, model: str, input_tokens: int,
                     output_tokens: int, cached_input_tokens: int = 0,
                     cost_usd: float = 0.0, message_id: str | None = None,
                     base_url: str | None = None) -> int:
        """One usage_log row; also accumulates the session token/cost totals."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                cur = self._conn.execute(
                    "INSERT INTO usage_log (session_id, message_id, model, base_url,"
                    " input_tokens, output_tokens, cached_input_tokens, cost_usd)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    (session_id, message_id, model, base_url, int(input_tokens),
                     int(output_tokens), int(cached_input_tokens), float(cost_usd)))
                row_id = int(cur.lastrowid)
                self._conn.execute(
                    "UPDATE sessions SET"
                    " total_input_tokens = total_input_tokens + ?,"
                    " total_output_tokens = total_output_tokens + ?,"
                    " total_cost_usd = total_cost_usd + ?,"
                    " updated_at = datetime('now') WHERE id=?",
                    (int(input_tokens), int(output_tokens), float(cost_usd),
                     session_id))
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        return row_id

    def recent_messages(self, *, since: str, until: str | None = None,
                        limit: int = 200,
                        roles: tuple[str, ...] = ("user",)) -> list[dict]:
        """按时间窗跨会话读消息（带会话标题与时间），新→旧。

        2026-09-25：用户问「我今天问了什么问题」时，关键词检索答不了这个 ——
        消息正文里没有「今天」这个词，要的是按时间列出。时间一律按 UTC 比较
        （库里 created_at 是 datetime('now')，即 UTC），本地时间在调用方换算。
        """
        sql = ("SELECT m.session_id, m.role, m.created_at, m.content, s.title"
               " FROM messages m JOIN sessions s ON s.id = m.session_id"
               " WHERE m.created_at >= ? AND m.active = 1")
        params: list = [str(since)]
        if until:
            sql += " AND m.created_at <= ?"
            params.append(str(until))
        if roles:
            sql += " AND m.role IN (%s)" % ",".join("?" for _ in roles)
            params += list(roles)
        sql += " ORDER BY m.created_at DESC, m.ordinal DESC LIMIT ?"
        params.append(int(limit))
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [{
            "sessionId": r["session_id"],
            "sessionTitle": r["title"] or "",
            "role": r["role"],
            "createdAt": r["created_at"],
            "content": (r["content"] or "")[:400],
        } for r in rows]

    # ---------------------------------------------------------------- search
    def search_messages(self, query: str, *, session_id: str | None = None,
                        limit: int = 50) -> list[dict]:
        """FTS5 trigram substring search over message content.

        Joins back through messages_fts_trigram_src so each hit carries
        sessionId, ordinal, and role. NULL content is never indexed.
        """
        if not query:
            return []
        match = " ".join(_quote_phrase(term) for term in query.split() if term)
        if not match:
            return []
        sql = (
            "SELECT m.session_id, m.ordinal, m.role, m.content,"
            " snippet(messages_fts, 0, '[', ']', '...', 8) AS snippet"
            " FROM messages_fts f"
            " JOIN messages_fts_trigram_src s ON s.src_id = f.rowid"
            " JOIN messages m ON m.session_id = s.session_id AND m.ordinal = s.ordinal"
            " WHERE messages_fts MATCH ?"
        )
        params: list = [match]
        if session_id is not None:
            sql += " AND m.session_id = ?"
            params.append(session_id)
        sql += " ORDER BY m.session_id, m.ordinal LIMIT ?"
        params.append(int(limit))
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [{"sessionId": r["session_id"], "ordinal": int(r["ordinal"]),
                 "role": r["role"], "snippet": r["snippet"]} for r in rows]


def _has_table_row(conn: sqlite3.Connection, table: str, key: str) -> bool:
    row = conn.execute("SELECT 1 FROM %s WHERE id=?" % table, (key,)).fetchone()
    return row is not None


def title_from_messages(messages: list[dict]) -> str:
    """First user message's text, clipped to TITLE_MAX_CHARS (30 chars)."""
    for msg in messages or []:
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            text = content.strip()
        elif isinstance(content, list):
            text = " ".join(
                (b.get("text") or "") for b in content
                if isinstance(b, dict) and b.get("type") == "text").strip()
        else:
            continue
        if text:
            return text[:TITLE_MAX_CHARS]
        break
    return "New session"
