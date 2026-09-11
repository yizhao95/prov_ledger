-- Migration 014: node_reason / expectations / outcomes — the "why" and the
-- "did it work" that no analysis can recover. Append-only by trigger.
--
-- node_reason: one row per (data point, run) answer to "why did this change";
--   text NULL == explicitly UNSTATED (never fabricated). kind 'rejected_path'
--   may be unanchored (node_key NULL); every other kind must name a node_key.
--   Reasons are keyed by the PSG node_key (analyzer.history), so they survive
--   renames/moves of the function or column they are about.
-- expectations / outcomes (spec §3.1): what a plan claimed a change would do,
--   and what was later observed (profile drift), derived (survival signal) or
--   honestly unavailable (none_available). Never updated, never deleted.
-- Plans.review_skip_reason: when review_and_complete decides NOT to review a
--   plan, it says why here (and in the review step log) — never silently.

CREATE TABLE IF NOT EXISTS node_reason (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    node_key   TEXT,
    project    TEXT NOT NULL,
    run_id     INTEGER,
    plan_id    TEXT NOT NULL,
    step_id    TEXT,
    kind       TEXT NOT NULL CHECK (kind IN ('reason','rejected_path','constraint_ref')),
    text       TEXT,
    source     TEXT NOT NULL CHECK (source IN ('agent','human','system')),
    tier       TEXT NOT NULL CHECK (tier IN ('stated','asserted','derived')),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now')),
    CHECK (node_key IS NOT NULL OR kind = 'rejected_path')
);
CREATE INDEX IF NOT EXISTS idx_node_reason_key ON node_reason (node_key, created_at);
CREATE INDEX IF NOT EXISTS idx_node_reason_plan ON node_reason (project, plan_id);
CREATE TRIGGER IF NOT EXISTS trg_node_reason_no_update BEFORE UPDATE ON node_reason
BEGIN SELECT RAISE(ABORT, 'node_reason is append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_node_reason_no_delete BEFORE DELETE ON node_reason
BEGIN SELECT RAISE(ABORT, 'node_reason is append-only'); END;

CREATE TABLE IF NOT EXISTS expectations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id     TEXT NOT NULL,
    step_id     TEXT,
    project     TEXT NOT NULL,
    target      TEXT NOT NULL,
    target_kind TEXT NOT NULL CHECK (target_kind IN ('node','column','dataset','metric')),
    claim       TEXT NOT NULL,
    channel     TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_expectations_project_plan ON expectations (project, plan_id);
CREATE TRIGGER IF NOT EXISTS trg_expectations_no_update BEFORE UPDATE ON expectations
BEGIN SELECT RAISE(ABORT, 'expectations is append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_expectations_no_delete BEFORE DELETE ON expectations
BEGIN SELECT RAISE(ABORT, 'expectations is append-only'); END;

CREATE TABLE IF NOT EXISTS outcomes (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    expectation_id     INTEGER NOT NULL REFERENCES expectations(id),
    kind               TEXT NOT NULL CHECK (kind IN ('observed','survival','none_available')),
    value_json         TEXT NOT NULL,
    source             TEXT NOT NULL,
    tier               TEXT NOT NULL CHECK (tier IN ('observed','derived','none')),
    reason             TEXT,
    backfilled_by_plan TEXT,
    observed_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_outcomes_expectation ON outcomes (expectation_id, kind);
CREATE TRIGGER IF NOT EXISTS trg_outcomes_no_update BEFORE UPDATE ON outcomes
BEGIN SELECT RAISE(ABORT, 'outcomes is append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_outcomes_no_delete BEFORE DELETE ON outcomes
BEGIN SELECT RAISE(ABORT, 'outcomes is append-only'); END;

ALTER TABLE Plans ADD COLUMN review_skip_reason TEXT;
