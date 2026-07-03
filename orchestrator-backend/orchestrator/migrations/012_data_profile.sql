-- Migration 012: data_profile — runtime snapshots of a dataset's shape (Phase 1).
--
-- Fills the "runtime-probe" provenance the static analyzer anticipates: each row
-- is one column of one dataset as observed at runtime (dtype, null fraction, row
-- count, distinct count). `project` is the instance discriminator so multiple
-- projects can share one orchestrator DB; `plan_id`/`step_id` tie a snapshot to
-- the run that produced it. Additive only.

CREATE TABLE IF NOT EXISTS data_profile (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    project        TEXT,
    plan_id        TEXT,
    step_id        TEXT,
    dataset        TEXT NOT NULL,
    column_name    TEXT NOT NULL,
    dtype          TEXT,
    null_frac      REAL,
    row_count      INTEGER,
    distinct_count INTEGER,
    observed_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_data_profile_ds
    ON data_profile (project, dataset, column_name);
