-- 001_initial.sql
-- Baseline schema for the session store (sessions.db).
-- Text is copied verbatim from the design doc (desktop-agent-app-design.md
-- section "SQLite table design"). Column names, types, and defaults must not
-- change: downstream code and the 004 migration build on this exact shape.
CREATE TABLE schema_version (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT (datetime('now')));
CREATE TABLE sessions (id TEXT PRIMARY KEY, title TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT (datetime('now')), updated_at TEXT NOT NULL DEFAULT (datetime('now')), model TEXT NOT NULL, base_url TEXT, status TEXT NOT NULL DEFAULT 'active', total_input_tokens INTEGER NOT NULL DEFAULT 0, total_output_tokens INTEGER NOT NULL DEFAULT 0, total_cost_usd REAL NOT NULL DEFAULT 0);
CREATE INDEX idx_sessions_updated ON sessions(updated_at DESC);
CREATE TABLE messages (id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE, role TEXT NOT NULL, content TEXT, tool_call_id TEXT, tool_calls_json TEXT, created_at TEXT NOT NULL DEFAULT (datetime('now')), ordinal INTEGER NOT NULL);
CREATE INDEX idx_messages_session ON messages(session_id, ordinal);
CREATE TABLE tool_calls (id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE, message_id TEXT NOT NULL REFERENCES messages(id) ON DELETE CASCADE, tool_name TEXT NOT NULL, arguments_json TEXT NOT NULL, result_json TEXT, error_json TEXT, permission_level TEXT NOT NULL, decision TEXT NOT NULL, duration_ms INTEGER, started_at TEXT NOT NULL, finished_at TEXT);
CREATE INDEX idx_tool_calls_session ON tool_calls(session_id);
CREATE TABLE usage_log (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT REFERENCES sessions(id) ON DELETE CASCADE, message_id TEXT REFERENCES messages(id) ON DELETE SET NULL, model TEXT NOT NULL, base_url TEXT, input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL, cached_input_tokens INTEGER NOT NULL DEFAULT 0, cost_usd REAL NOT NULL, request_at TEXT NOT NULL DEFAULT (datetime('now')));
CREATE INDEX idx_usage_log_session ON usage_log(session_id);
CREATE INDEX idx_usage_log_request_at ON usage_log(request_at);
CREATE INDEX idx_usage_log_model ON usage_log(model);
