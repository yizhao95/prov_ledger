-- 023_ask.sql — DP phase 2e: the read-only question entry (`/ledger`, `provledger ask`).
--
-- One row per question asked, written once, at the end of the ask: the
-- candidates the CODE found, the nodes the MODEL picked among them, the sha of
-- the fact table the code computed, the answer the model wrote, the ids it
-- cited, the scope line and what was dropped from the answer. Nothing here is
-- a business fact — it is the trace of one read, kept so a wrong answer can be
-- replayed and used as a calibration set (spec §21).
CREATE TABLE IF NOT EXISTS ask_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    project         TEXT NOT NULL,
    question        TEXT NOT NULL,
    candidates_json TEXT,
    chosen_json     TEXT,
    facts_sha       TEXT,
    answer          TEXT,
    cites_json      TEXT,
    scope_json      TEXT,
    dropped_json    TEXT,
    model           TEXT,
    runner          TEXT,
    at              TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_ask_log_project ON ask_log (project, id);
CREATE TRIGGER IF NOT EXISTS trg_ask_log_no_update BEFORE UPDATE ON ask_log
BEGIN SELECT RAISE(ABORT, 'ask_log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_ask_log_no_delete BEFORE DELETE ON ask_log
BEGIN SELECT RAISE(ABORT, 'ask_log is append-only'); END;

-- ask_feedback: a person's word on one answer. The calibration set for node
-- selection; never edited, a second opinion is a second row.
CREATE TABLE IF NOT EXISTS ask_feedback (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ask_id  INTEGER NOT NULL REFERENCES ask_log(id),
    verdict TEXT NOT NULL CHECK (verdict IN ('wrong', 'partial', 'right')),
    note    TEXT,
    by      TEXT NOT NULL DEFAULT 'human',
    at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_ask_feedback_ask ON ask_feedback (ask_id, id);
CREATE TRIGGER IF NOT EXISTS trg_ask_feedback_no_update BEFORE UPDATE ON ask_feedback
BEGIN SELECT RAISE(ABORT, 'ask_feedback is append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_ask_feedback_no_delete BEFORE DELETE ON ask_feedback
BEGIN SELECT RAISE(ABORT, 'ask_feedback is append-only'); END;
