-- Migration 010: data-model enrichment (additive only; no table rebuild).
-- Restores step_type enforcement (via trigger) + idx_steps_type that migration
-- 007 dropped, adds lifecycle/audit columns, a Deviations history table, and
-- ledger provenance columns. All changes are additive so the migration runner
-- (which applies each file exactly once) keeps it safe and idempotent.

-- BE-D3: first-class failure reason + retry count on Steps.
ALTER TABLE Steps ADD COLUMN failure_reason TEXT;
ALTER TABLE Steps ADD COLUMN attempt_count INTEGER NOT NULL DEFAULT 0;

-- BE-D2: lifecycle timestamps for staleness + incremental refresh.
ALTER TABLE Steps ADD COLUMN updated_at TEXT;
ALTER TABLE Plans ADD COLUMN updated_at TEXT;

-- BE-D4: park-for-review state, distinct from a normal IN_PROGRESS plan.
ALTER TABLE Plans ADD COLUMN review_state TEXT
    CHECK (review_state IS NULL OR review_state IN ('awaiting_agent', 'reviewed'));

-- BE-C3: restore the step_type index dropped by migration 007.
CREATE INDEX IF NOT EXISTS idx_steps_type ON Steps(step_type);

-- BE-C2: restore DB-level step_type enforcement that 007 lost. Triggers mirror
-- orchestrator.db.VALID_STEP_TYPES (kept in sync by test_010 + the enum drift
-- guard). Using triggers avoids a risky full Steps table rebuild.
CREATE TRIGGER IF NOT EXISTS trg_steps_type_insert
BEFORE INSERT ON Steps
WHEN NEW.step_type IS NOT NULL AND NEW.step_type NOT IN
    ('THINKING', 'ANALYSIS', 'CODE', 'COMMAND', 'DOCUMENTATION', 'SUB_AGENT')
BEGIN
    SELECT RAISE(ABORT, 'invalid step_type');
END;

CREATE TRIGGER IF NOT EXISTS trg_steps_type_update
BEFORE UPDATE OF step_type ON Steps
WHEN NEW.step_type IS NOT NULL AND NEW.step_type NOT IN
    ('THINKING', 'ANALYSIS', 'CODE', 'COMMAND', 'DOCUMENTATION', 'SUB_AGENT')
BEGIN
    SELECT RAISE(ABORT, 'invalid step_type');
END;

-- BE-D1: deviation / revision history — the durable "why a plan changed" trail.
CREATE TABLE IF NOT EXISTS Deviations (
    deviation_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id          TEXT NOT NULL,
    target_step_id   TEXT,
    justification    TEXT NOT NULL,
    new_step_ids     TEXT,                 -- JSON list of inserted sub-step ids
    revision_count   INTEGER,
    created_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now')),
    FOREIGN KEY (plan_id) REFERENCES Plans(plan_id),
    FOREIGN KEY (target_step_id) REFERENCES Steps(step_id)
);
CREATE INDEX IF NOT EXISTS idx_deviations_plan ON Deviations(plan_id);

-- SK-D1: ledger provenance + relevance-ranking columns.
ALTER TABLE LedgerEntries ADD COLUMN superseded_by INTEGER;
ALTER TABLE LedgerEntries ADD COLUMN updated_at TEXT;
ALTER TABLE LedgerEntries ADD COLUMN plan_id TEXT;
ALTER TABLE LedgerEntries ADD COLUMN hit_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE LedgerEntries ADD COLUMN last_matched_at TEXT;
