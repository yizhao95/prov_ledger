-- 037_task_evidence.sql — evidence follows the task, and a source keeps its
-- words (task-level redesign, step 3b).
--
-- reference was a pointer only: a label, a date, a uri (018). The root of a task
-- is usually a sentence somebody said in a meeting or a mail, and a pointer alone
-- rots — the link moves, the mailbox is closed, the notes are rewritten. So a
-- reference keeps who said it (`said_by`) and what they said (`excerpt`), as much
-- as the agent that found it judged was needed, never more than 2000 characters:
-- the passage, not the document. Both are nullable — a pointer with no words is
-- still a pointer — and both are inside the hash chain for rows written from
-- here on. Rows written before 037 were hashed without them, read NULL, and
-- verify in that older form (provenance._PRE_COLUMN).
--
-- The update whitelist is recreated so it names the two new columns: a trigger
-- that lists the columns which may not change says nothing about a column added
-- after it, and the words a source carried must not be rewritten later.
--
-- evidence_log recorded one ending per changed node (031). Evidence is looked for
-- once per task now — the task's root, not each function it touched — so a row
-- may name the task reason instead of a node. SQLite cannot loosen NOT NULL in
-- place: the table is rebuilt, every row copied with its id. Nothing points at
-- evidence_log and no view reads it, so no PRAGMA dance is needed.

ALTER TABLE reference ADD COLUMN said_by TEXT
    CHECK (said_by IS NULL OR length(said_by) BETWEEN 1 AND 200);
ALTER TABLE reference ADD COLUMN excerpt TEXT
    CHECK (excerpt IS NULL OR length(excerpt) BETWEEN 1 AND 2000);

DROP TRIGGER IF EXISTS trg_reference_update_whitelist;
CREATE TRIGGER trg_reference_update_whitelist BEFORE UPDATE ON reference
WHEN NEW.id IS NOT OLD.id OR NEW.project IS NOT OLD.project OR NEW.kind IS NOT OLD.kind OR NEW.uri IS NOT OLD.uri
  OR NEW.label IS NOT OLD.label OR NEW.occurred_at IS NOT OLD.occurred_at OR NEW.registered_at IS NOT OLD.registered_at
  OR NEW.verifiability IS NOT OLD.verifiability OR NEW.visibility IS NOT OLD.visibility
  OR NEW.said_by IS NOT OLD.said_by OR NEW.excerpt IS NOT OLD.excerpt
  OR NEW.prev_hash IS NOT OLD.prev_hash OR NEW.hash IS NOT OLD.hash
BEGIN SELECT RAISE(ABORT, 'reference: only last_checked may change'); END;

CREATE TABLE evidence_log_new (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id        TEXT NOT NULL,
    node_key       TEXT,                          -- the changed node of a slot from before 037
    reason_id      INTEGER REFERENCES change_reason(id),   -- the task reason of a slot from 037 on
    reason_tier    TEXT CHECK (reason_tier IS NULL OR reason_tier IN ('stated', 'asserted', 'derived', 'unstated')),
    evidence_level TEXT CHECK (evidence_level IS NULL OR evidence_level IN ('linked', 'verbal', 'task_context', 'unstated')),
    searched       INTEGER NOT NULL CHECK (searched IN (0, 1)),
    tool_hint      TEXT,
    outcome        TEXT NOT NULL CHECK (outcome IN ('attached', 'found_nothing', 'timed_out', 'not_searched')),
    elapsed_ms     INTEGER CHECK (elapsed_ms IS NULL OR elapsed_ms >= 0),
    at             TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now')),
    CHECK ((outcome = 'not_searched') = (searched = 0)),
    CHECK (node_key IS NOT NULL OR reason_id IS NOT NULL)
);
INSERT INTO evidence_log_new (id, plan_id, node_key, reason_id, reason_tier, evidence_level, searched, tool_hint,
                              outcome, elapsed_ms, at)
    SELECT id, plan_id, node_key, NULL, reason_tier, evidence_level, searched, tool_hint, outcome, elapsed_ms, at
    FROM evidence_log ORDER BY id;
DROP TABLE evidence_log;
ALTER TABLE evidence_log_new RENAME TO evidence_log;

CREATE INDEX IF NOT EXISTS idx_evidence_log_plan ON evidence_log (plan_id, node_key, id);
CREATE INDEX IF NOT EXISTS idx_evidence_log_reason ON evidence_log (reason_id, id) WHERE reason_id IS NOT NULL;
CREATE TRIGGER IF NOT EXISTS trg_evidence_log_no_update BEFORE UPDATE ON evidence_log
BEGIN SELECT RAISE(ABORT, 'evidence_log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_evidence_log_no_delete BEFORE DELETE ON evidence_log
BEGIN SELECT RAISE(ABORT, 'evidence_log is append-only'); END;
