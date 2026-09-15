-- Migration 018: decision provenance (DP phase 0 + 1).
--
-- Phase 0 (Task 0): tool_call_log — one row per tool call, written by the
-- PostToolUse hook (orchestrator.hooks). plan_metrics attributes rows to a
-- plan by its created_at..completed_at window and the project's repo (cwd),
-- so every phase can answer "which tool call did this cost" (north star:
-- the tool must not silently get slower). Append-only like node_reason.
-- Phase 1 (Task 1) adds utterance / reference / change_reason /
-- reference_link / trigger_log / migration_state, the views and the
-- UPDATE whitelist triggers to this same file.
CREATE TABLE IF NOT EXISTS tool_call_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL,
    cwd         TEXT,
    tool_name   TEXT NOT NULL,
    at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_tool_call_log_at ON tool_call_log (at);
CREATE TRIGGER IF NOT EXISTS trg_tool_call_log_no_update BEFORE UPDATE ON tool_call_log
BEGIN SELECT RAISE(ABORT, 'tool_call_log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_tool_call_log_no_delete BEFORE DELETE ON tool_call_log
BEGIN SELECT RAISE(ABORT, 'tool_call_log is append-only'); END;
