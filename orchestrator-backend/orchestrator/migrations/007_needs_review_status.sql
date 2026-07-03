-- Migration 007: Add NEEDS_REVIEW to the Steps.status CHECK constraint
--
-- The LLM-reviewed-plan-completion feature introduces a new non-terminal step
-- state, NEEDS_REVIEW, used by the auto-appended review step when a plan that
-- mentions a registered project finishes all its regular steps. The review step
-- waits in NEEDS_REVIEW until a coding-agent sub-agent (update-project-state-graph
-- skill) reviews the project's git diff against the deep state graph and then
-- closes the plan via agent-review-close.sh (NEEDS_REVIEW -> COMPLETED|FAILED).
--
-- SQLite cannot ALTER an existing CHECK constraint, so we rebuild the Steps
-- table. This migration is additive + idempotent (guarded by schema_version in
-- run_migrations) and preserves all existing rows, indexes, and the is_review
-- column added by migration 006.
--
-- Strategy: rename old table -> create new table with widened CHECK -> copy rows
-- -> drop old -> recreate indexes. PRAGMA foreign_keys is toggled off for the
-- rebuild so the self-referential FK on parent_step_id doesn't block the swap.

PRAGMA foreign_keys = OFF;
-- legacy_alter_table=ON makes RENAME TABLE leave child-table FK references
-- (e.g. SkillActivations -> Steps from migration 005) pointing at the ORIGINAL
-- name instead of auto-rewriting them to Steps_old_007. Without this, dropping
-- the old table leaves dangling FKs and breaks every later INSERT into Steps.
PRAGMA legacy_alter_table = ON;

ALTER TABLE Steps RENAME TO Steps_old_007;

CREATE TABLE Steps (
    step_id          TEXT PRIMARY KEY,
    plan_id          TEXT NOT NULL,
    parent_step_id   TEXT,
    description      TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'PENDING'
                     CHECK(status IN ('PENDING','STARTING','IN_PROGRESS',
                                      'NEEDS_REVIEW','COMPLETED','FAILED')),
    execution_order  INTEGER NOT NULL,
    depth_level      INTEGER NOT NULL DEFAULT 0,
    log_context      TEXT NOT NULL DEFAULT '',
    started_at       TEXT,
    completed_at     TEXT,
    step_type        TEXT,
    agent_input      TEXT,
    agent_output     TEXT,
    summary          TEXT,
    is_review        INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (plan_id) REFERENCES Plans(plan_id),
    FOREIGN KEY (parent_step_id) REFERENCES Steps(step_id)
);

-- Copy every column that exists on the old table. Columns added by migrations
-- 002/004/006 are all present here; the INSERT lists them explicitly.
INSERT INTO Steps (
    step_id, plan_id, parent_step_id, description, status, execution_order,
    depth_level, log_context, started_at, completed_at, step_type,
    agent_input, agent_output, summary, is_review
)
SELECT
    step_id, plan_id, parent_step_id, description, status, execution_order,
    depth_level, log_context, started_at, completed_at, step_type,
    agent_input, agent_output, summary, is_review
FROM Steps_old_007;

DROP TABLE Steps_old_007;

CREATE INDEX IF NOT EXISTS idx_steps_plan ON Steps(plan_id, execution_order);
CREATE INDEX IF NOT EXISTS idx_steps_parent ON Steps(parent_step_id);
CREATE INDEX IF NOT EXISTS idx_steps_plan_review ON Steps (plan_id, is_review);

PRAGMA legacy_alter_table = OFF;
PRAGMA foreign_keys = ON;
