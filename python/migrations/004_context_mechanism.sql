-- 004_context_mechanism.sql
-- Context-mechanism v1 additions (desktop-agent-context-mechanism-v1.md,
-- section "Data structures and DDL"). Applied on top of 001_initial.sql.
--
-- Three ALTER statements, compaction_records, and spilled_outputs are copied
-- from the design doc. The FTS5 part is the doc's trigram virtual table plus
-- its three sync triggers, with one mandatory adjustment the doc itself calls
-- out: an FTS5 external-content table needs a *globally unique integer* rowid,
-- while messages.ordinal only unique per session. Feeding ordinal straight in
-- would make two sessions overwrite each other's index rows. The doc's remedy
-- ("project onto a content table / rowid mapping") lands here as the
-- messages_fts_trigram_src mapping table: it maps (session_id, ordinal) to an
-- AUTOINCREMENT integer src_id, and the virtual table points at that table.
--
-- Session state columns (spec: persisted window + overflow retry counter):
ALTER TABLE sessions ADD COLUMN context_length INTEGER NOT NULL;
ALTER TABLE sessions ADD COLUMN overflow_attempts INTEGER NOT NULL DEFAULT 0;

-- Message visibility flags: compaction flips bits, originals are never deleted.
ALTER TABLE messages ADD COLUMN active INTEGER NOT NULL DEFAULT 1;
ALTER TABLE messages ADD COLUMN compacted INTEGER NOT NULL DEFAULT 0;

-- (session_id, ordinal) must be unique for the FTS rowid mapping to resolve.
CREATE UNIQUE INDEX idx_messages_session_ordinal ON messages(session_id, ordinal);

-- Compaction audit: one row per compaction (who, how much, saved, generation).
CREATE TABLE compaction_records (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  generation INTEGER NOT NULL,          -- generation of the summary (dsh replaceGeneration style)
  trigger TEXT NOT NULL,                -- 'pressure' | 'context-overflow' (mount point 1/2)
  range_start_ordinal INTEGER,          -- masked range start (inclusive)
  range_end_ordinal INTEGER,            -- masked range end (inclusive)
  tokens_before INTEGER NOT NULL,
  tokens_after INTEGER NOT NULL,        -- effectiveness rule: after < before * 0.95
  summary_text TEXT NOT NULL,           -- mid-section summary (part of soft archive)
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(session_id, generation)
);
CREATE INDEX idx_compaction_session ON compaction_records(session_id, generation);

-- Spill file references: index of large outputs (L1 spill) written to disk.
CREATE TABLE spilled_outputs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  tool_call_id TEXT REFERENCES tool_calls(id) ON DELETE SET NULL,
  path TEXT NOT NULL,                   -- absolute disk path (userData/spill/<session>/<uuid>.txt)
  bytes INTEGER NOT NULL,               -- exact byte count (part of the locator)
  sha256 TEXT NOT NULL,                 -- integrity check
  preview_head TEXT NOT NULL,           -- head preview (what enters the context)
  preview_tail TEXT NOT NULL,           -- tail preview
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_spilled_session ON spilled_outputs(session_id);

-- FTS5 trigram virtual table (CJK substring search; the default tokenizer
-- returns 0 hits on Chinese and fails silently). Same shape as the design
-- doc's FTS_TRIGRAM_SQL, but the external content table is the rowid mapping
-- table below, NOT messages itself (see header note).
CREATE TABLE messages_fts_trigram_src (
  src_id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT NOT NULL,
  ordinal INTEGER NOT NULL,
  content TEXT,
  UNIQUE(session_id, ordinal)
);
CREATE VIRTUAL TABLE messages_fts USING fts5(
  content,
  content='messages_fts_trigram_src',  -- external content: the index never copies the original text
  content_rowid='src_id',              -- globally unique integer rowid via the mapping table
  tokenize='trigram'
);

-- Triggers (sync index rows on insert/delete/update). Each one keeps
-- messages_fts_trigram_src and messages_fts in lockstep with messages.
CREATE TRIGGER messages_ai AFTER INSERT ON messages BEGIN
  INSERT INTO messages_fts_trigram_src (session_id, ordinal, content)
    VALUES (new.session_id, new.ordinal, new.content);
  INSERT INTO messages_fts (rowid, content)
    VALUES (last_insert_rowid(), new.content);
END;
CREATE TRIGGER messages_ad AFTER DELETE ON messages BEGIN
  INSERT INTO messages_fts (messages_fts, rowid, content)
    VALUES ('delete',
      (SELECT src_id FROM messages_fts_trigram_src
        WHERE session_id=old.session_id AND ordinal=old.ordinal),
      old.content);
  DELETE FROM messages_fts_trigram_src
    WHERE session_id=old.session_id AND ordinal=old.ordinal;
END;
CREATE TRIGGER messages_au AFTER UPDATE ON messages BEGIN
  INSERT INTO messages_fts (messages_fts, rowid, content)
    VALUES ('delete',
      (SELECT src_id FROM messages_fts_trigram_src
        WHERE session_id=old.session_id AND ordinal=old.ordinal),
      old.content);
  DELETE FROM messages_fts_trigram_src
    WHERE session_id=old.session_id AND ordinal=old.ordinal;
  INSERT INTO messages_fts_trigram_src (session_id, ordinal, content)
    VALUES (new.session_id, new.ordinal, new.content);
  INSERT INTO messages_fts (rowid, content)
    VALUES (last_insert_rowid(), new.content);
END;
