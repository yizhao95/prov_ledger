-- Migration 015: LedgerEntries gains kind='constraint' (an externally imposed
-- rule anchored to data points), why_ref (a pointer to the source of the WHY —
-- meeting notes, decision doc) and why_visibility (shared|restricted). What
-- the constraint IS must be shared; WHY it exists may be restricted, in which
-- case only why_ref leaves the ledger. The inline CHECK from 009 forces a
-- table rebuild (SQLite cannot alter a CHECK in place).
-- subjects may carry PSG node_keys (nk_…) or owner.column names: a constraint
-- anchored to a node_key is matched EXACTLY by ledger_store.constraints_for,
-- never lexically. No category taxonomy lives in this table (E4-4): anything
-- beyond `kind` goes into free keywords.
CREATE TABLE LedgerEntries_new (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    project         TEXT NOT NULL,
    kind            TEXT NOT NULL CHECK (kind IN ('decision', 'anti_pattern', 'constraint')),
    subjects        TEXT,
    keywords        TEXT,
    statement       TEXT NOT NULL,
    rationale       TEXT,
    status          TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'superseded')),
    source          TEXT,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now')),
    superseded_by   INTEGER,
    updated_at      TEXT,
    plan_id         TEXT,
    hit_count       INTEGER NOT NULL DEFAULT 0,
    last_matched_at TEXT,
    why_ref         TEXT,
    why_visibility  TEXT NOT NULL DEFAULT 'shared' CHECK (why_visibility IN ('shared', 'restricted'))
);
INSERT INTO LedgerEntries_new (id, project, kind, subjects, keywords, statement, rationale, status, source,
                               created_at, superseded_by, updated_at, plan_id, hit_count, last_matched_at)
    SELECT id, project, kind, subjects, keywords, statement, rationale, status, source,
           created_at, superseded_by, updated_at, plan_id, hit_count, last_matched_at FROM LedgerEntries;
DROP TABLE LedgerEntries;
ALTER TABLE LedgerEntries_new RENAME TO LedgerEntries;
CREATE INDEX IF NOT EXISTS idx_ledger_project_status ON LedgerEntries (project, status);
CREATE INDEX IF NOT EXISTS idx_ledger_project_kind ON LedgerEntries (project, kind, status);
