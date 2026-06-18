-- Phase 1 orchestration schema (per Phase 4 spec)
-- Source of truth for all plans + steps. Markdown plan files become a regenerated VIEW.

CREATE TABLE IF NOT EXISTS Plans (
    plan_id          TEXT PRIMARY KEY,
    original_goal    TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'IN_PROGRESS'
                     CHECK(status IN ('IN_PROGRESS','COMPLETED','FAILED')),
    revision_count   INTEGER NOT NULL DEFAULT 0,
    max_revisions    INTEGER NOT NULL DEFAULT 5,
    created_at       TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at     TEXT
);

CREATE TABLE IF NOT EXISTS Steps (
    step_id          TEXT PRIMARY KEY,
    plan_id          TEXT NOT NULL,
    parent_step_id   TEXT,
    description      TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'PENDING'
                     CHECK(status IN ('PENDING','STARTING','IN_PROGRESS','COMPLETED','FAILED')),
    execution_order  INTEGER NOT NULL,
    depth_level      INTEGER NOT NULL DEFAULT 0,
    log_context      TEXT NOT NULL DEFAULT '',
    started_at       TEXT,
    completed_at     TEXT,
    FOREIGN KEY (plan_id) REFERENCES Plans(plan_id),
    FOREIGN KEY (parent_step_id) REFERENCES Steps(step_id)
);

CREATE INDEX IF NOT EXISTS idx_steps_plan ON Steps(plan_id, execution_order);
CREATE INDEX IF NOT EXISTS idx_steps_parent ON Steps(parent_step_id);

CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY);
INSERT OR IGNORE INTO schema_version (version) VALUES (1);
