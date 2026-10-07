-- 033_plan_abandoned.sql — a plan can be ABANDONED (FL-216).
--
-- A publish that failed half-way left a plan IN_PROGRESS with no steps, and the
-- only close available (finish-plan) would have called it COMPLETED. abandon-plan
-- puts down a plan nobody started; it needs a status that says so, and
-- Plans.status was CHECKed to IN_PROGRESS / COMPLETED / FAILED.
--
-- SQLite cannot widen a CHECK in place, so Plans is rebuilt the way 007 rebuilt
-- Steps: rename the old table, create the new one with the widened CHECK and every
-- column the later migrations added (their CHECKs kept), copy the rows, drop the
-- old table, recreate the two indexes. Steps, SkillActivations and Deviations point
-- at Plans; foreign_keys is off for the swap, and legacy_alter_table keeps their
-- references on the name Plans instead of following the rename.

PRAGMA foreign_keys = OFF;
PRAGMA legacy_alter_table = ON;

ALTER TABLE Plans RENAME TO Plans_old_033;

CREATE TABLE Plans (
    plan_id            TEXT PRIMARY KEY,
    original_goal      TEXT NOT NULL,
    status             TEXT NOT NULL DEFAULT 'IN_PROGRESS'
                       CHECK (status IN ('IN_PROGRESS', 'COMPLETED', 'FAILED', 'ABANDONED')),
    revision_count     INTEGER NOT NULL DEFAULT 0,
    max_revisions      INTEGER NOT NULL DEFAULT 5,
    created_at         TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at       TEXT,
    user_query         TEXT,
    impact_context     TEXT,
    updated_at         TEXT,
    review_state       TEXT
                       CHECK (review_state IS NULL OR review_state IN ('awaiting_agent', 'reviewed')),
    review_skip_reason TEXT,
    project            TEXT,
    project_source     TEXT
                       CHECK (project_source IS NULL OR project_source IN ('declared', 'cwd', 'legacy')),
    headline_json      TEXT,
    session_id         TEXT
);

INSERT INTO Plans (
    plan_id, original_goal, status, revision_count, max_revisions, created_at, completed_at,
    user_query, impact_context, updated_at, review_state, review_skip_reason, project,
    project_source, headline_json, session_id
)
SELECT
    plan_id, original_goal, status, revision_count, max_revisions, created_at, completed_at,
    user_query, impact_context, updated_at, review_state, review_skip_reason, project,
    project_source, headline_json, session_id
FROM Plans_old_033;

DROP TABLE Plans_old_033;

CREATE INDEX IF NOT EXISTS idx_plans_project ON Plans (project, status);
CREATE INDEX IF NOT EXISTS idx_plans_session ON Plans (session_id);

PRAGMA legacy_alter_table = OFF;
PRAGMA foreign_keys = ON;
