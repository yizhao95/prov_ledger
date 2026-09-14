-- Migration 017: metrics — append-only NUMERIC observations (phase 7).
-- A metric row is something a run measured (a rollup's mean net revenue, a
-- model's AUC, a row count), recorded through record-metric / run-step
-- metrics_from_stdout / a demo — never typed in by a model. The metric
-- outcome channel judges an expectation `metric:<name>` against the nearest
-- row before and after the expectation was made (outcome_channels.MetricChannel).
-- value must be a real number (the CHECK refuses NULL/NaN-ish text); the
-- triggers make the table append-only like expectations / outcomes.
CREATE TABLE IF NOT EXISTS metrics (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project     TEXT NOT NULL,
    plan_id     TEXT,
    step_id     TEXT,
    name        TEXT NOT NULL,
    value       REAL NOT NULL CHECK (typeof(value) IN ('real', 'integer')),
    unit        TEXT,
    source      TEXT NOT NULL,                       -- 'run-step' | 'record-metric' | 'demo' | ...
    observed_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_metrics_project_name ON metrics (project, name, observed_at);
CREATE TRIGGER IF NOT EXISTS trg_metrics_no_update BEFORE UPDATE ON metrics
BEGIN SELECT RAISE(ABORT, 'metrics is append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_metrics_no_delete BEFORE DELETE ON metrics
BEGIN SELECT RAISE(ABORT, 'metrics is append-only'); END;
