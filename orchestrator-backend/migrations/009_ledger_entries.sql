-- Migration 009: LedgerEntries — DE Decision-Memory (provLedger Phase E)
--
-- The "ledger track": the home for everything the gate track honestly CANNOT
-- check — semantic contracts, data-engineering rationale, and past failures.
-- Stores DECISIONS (+ rationale) and ANTI-PATTERNS (failures + cause), scoped to
-- a project and a set of subject symbols/tables + free keywords. At plan time a
-- deterministic fuzzy match surfaces relevant entries as REMINDERS (never blocks)
-- inside impact_context.ledger_reminders.
--
-- Gradual + opt-in by design: populated by MANUAL entries (ledger_cli.py),
-- grown from real decisions and real failures — never auto-populated.
--
--   kind       — 'decision' | 'anti_pattern'
--   subjects   — JSON list of symbols/tables this entry is about
--   keywords   — JSON list of free-text match terms
--   statement  — the decision/anti-pattern itself
--   rationale  — WHY (the memory worth keeping)
--   status     — 'active' | 'superseded'
--   source     — provenance of the entry (who/what recorded it)

CREATE TABLE IF NOT EXISTS LedgerEntries (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    project    TEXT NOT NULL,
    kind       TEXT NOT NULL CHECK (kind IN ('decision', 'anti_pattern')),
    subjects   TEXT,                       -- JSON list
    keywords   TEXT,                       -- JSON list
    statement  TEXT NOT NULL,
    rationale  TEXT,
    status     TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'superseded')),
    source     TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_ledger_project_status
    ON LedgerEntries (project, status);
