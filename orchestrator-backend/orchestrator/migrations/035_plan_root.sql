-- 035_plan_root.sql — a plan's root cause (task-level redesign, step 2).
--
-- A plan is a task. Why it exists usually started earlier: the user said once
-- what they wanted, and several plans carry it forward. One row per judgement:
--
--   kind new        this plan starts a root; root_plan_id = the plan itself, and
--                   utterance_id may point at the user's sentence that states it
--   kind continues  this plan carries an earlier plan's root forward;
--                   continues_plan_id = the plan it names, root_plan_id = where
--                   that root started
--   kind unknown    nobody said; root_plan_id is NULL
--
-- Whether two tasks share a root is a model's judgement (state asserted) until a
-- person confirms or rejects it. A confirmation is a new row, never an edit: the
-- latest row of a plan counts, and the earlier judgement stays readable.

CREATE TABLE IF NOT EXISTS plan_root (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id           TEXT NOT NULL,
    kind              TEXT NOT NULL CHECK (kind IN ('new', 'continues', 'unknown')),
    continues_plan_id TEXT,
    root_plan_id      TEXT,
    utterance_id      INTEGER,
    basis             TEXT,
    state             TEXT NOT NULL CHECK (state IN ('asserted', 'confirmed', 'rejected')),
    recorded_by       TEXT NOT NULL CHECK (recorded_by IN ('agent', 'human', 'system')),
    at                TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now')),
    CHECK ((kind = 'continues') = (continues_plan_id IS NOT NULL)),
    CHECK ((kind = 'unknown') = (root_plan_id IS NULL))
);

CREATE INDEX IF NOT EXISTS idx_plan_root_plan ON plan_root (plan_id, id);
CREATE INDEX IF NOT EXISTS idx_plan_root_root ON plan_root (root_plan_id, id);

CREATE TRIGGER IF NOT EXISTS trg_plan_root_no_update BEFORE UPDATE ON plan_root
BEGIN SELECT RAISE(ABORT, 'plan_root is append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_plan_root_no_delete BEFORE DELETE ON plan_root
BEGIN SELECT RAISE(ABORT, 'plan_root is append-only'); END;
