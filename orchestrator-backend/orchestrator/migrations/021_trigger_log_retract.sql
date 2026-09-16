-- 021_trigger_log_retract.sql — DP phase 2d (Task 0).
--
-- The UserPromptSubmit hook used to record Claude Code's OWN injected text
-- (`<task-notification>`, `<system-reminder>`, `<local-command-caveat>`,
-- `<command-name>`) as if the user had said it. hooks.is_injected_prompt now
-- refuses those prompts, but history only appends: the rows already in
-- `utterance` are NOT deleted. Instead each one gets a trigger_log row saying
-- it was retracted as an R0 candidate — which needs one more verdict.
--
-- SQLite cannot widen a CHECK in place, so trigger_log is rebuilt with the
-- same columns, the same indexes and the same append-only triggers. The
-- rebuild drops the triggers first (they would abort the copy's DELETE of the
-- old table) and restores them after. The enum keeps 019's 'ambiguous' — the
-- backend suite caught this file dropping it, which is exactly what the rebuild
-- pattern is dangerous for.
DROP TRIGGER IF EXISTS trg_trigger_log_no_update;
DROP TRIGGER IF EXISTS trg_trigger_log_no_delete;

CREATE TABLE trigger_log_new (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project     TEXT NOT NULL,
    plan_id     TEXT NOT NULL,
    node_key    TEXT,
    path        TEXT NOT NULL CHECK (path IN ('code', 'external')),
    rule_id     TEXT,
    verdict     TEXT NOT NULL CHECK (verdict IN ('auto', 'ask', 'silent', 'ambiguous', 'retract')),
    basis       TEXT,
    user_action TEXT,
    at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now'))
);
INSERT INTO trigger_log_new (id, project, plan_id, node_key, path, rule_id, verdict, basis, user_action, at)
    SELECT id, project, plan_id, node_key, path, rule_id, verdict, basis, user_action, at FROM trigger_log;
DROP TABLE trigger_log;
ALTER TABLE trigger_log_new RENAME TO trigger_log;

CREATE INDEX IF NOT EXISTS idx_trigger_log_plan ON trigger_log (project, plan_id);
CREATE TRIGGER IF NOT EXISTS trg_trigger_log_no_update BEFORE UPDATE ON trigger_log
BEGIN SELECT RAISE(ABORT, 'trigger_log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_trigger_log_no_delete BEFORE DELETE ON trigger_log
BEGIN SELECT RAISE(ABORT, 'trigger_log is append-only'); END;
