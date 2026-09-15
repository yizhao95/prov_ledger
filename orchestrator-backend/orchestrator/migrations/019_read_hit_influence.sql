-- Migration 019: decision provenance phase 2.
--
-- Part 1 (Task 0): trigger_log gains the verdict 'ambiguous' — R0 saw a sentence
-- that names too many nodes to mean one of them. SQLite cannot alter a CHECK, so
-- the table is rebuilt in place with every row and index kept (DROP TABLE is not
-- a DELETE: the append-only triggers do not fire, and they are recreated below).
-- Part 2 (Task 1) adds read_hit / influence / headline / headline_response /
-- session_run, the overhead columns and reason_stats_v.
CREATE TABLE IF NOT EXISTS trigger_log__new (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project     TEXT NOT NULL,
    plan_id     TEXT NOT NULL,
    node_key    TEXT,
    path        TEXT NOT NULL CHECK (path IN ('code', 'external')),
    rule_id     TEXT,
    verdict     TEXT NOT NULL CHECK (verdict IN ('auto', 'ask', 'silent', 'ambiguous')),
    basis       TEXT,
    user_action TEXT,
    at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now'))
);
INSERT INTO trigger_log__new (id, project, plan_id, node_key, path, rule_id, verdict, basis, user_action, at)
    SELECT id, project, plan_id, node_key, path, rule_id, verdict, basis, user_action, at FROM trigger_log;
DROP TRIGGER IF EXISTS trg_trigger_log_no_update;
DROP TRIGGER IF EXISTS trg_trigger_log_no_delete;
DROP TABLE trigger_log;
ALTER TABLE trigger_log__new RENAME TO trigger_log;
CREATE INDEX IF NOT EXISTS idx_trigger_log_plan ON trigger_log (project, plan_id);
CREATE TRIGGER IF NOT EXISTS trg_trigger_log_no_update BEFORE UPDATE ON trigger_log
BEGIN SELECT RAISE(ABORT, 'trigger_log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_trigger_log_no_delete BEFORE DELETE ON trigger_log
BEGIN SELECT RAISE(ABORT, 'trigger_log is append-only'); END;
