-- Migration 019: decision provenance phase 2.
--
-- Part 1 (Task 0): trigger_log gains the verdict 'ambiguous' — R0 saw a sentence
-- that names too many nodes to mean one of them. SQLite cannot alter a CHECK, so
-- the table is rebuilt in place with every row and index kept (DROP TABLE is not
-- a DELETE: the append-only triggers do not fire, and they are recreated below).
-- Part 2 (Task 1) adds read_hit / influence / headline / headline_response /
-- session_run, the overhead columns and reason_stats_v.
CREATE TABLE IF NOT EXISTS trigger_log__new (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project     TEXT NOT NULL,
    plan_id     TEXT NOT NULL,
    node_key    TEXT,
    path        TEXT NOT NULL CHECK (path IN ('code', 'external')),
    rule_id     TEXT,
    verdict     TEXT NOT NULL CHECK (verdict IN ('auto', 'ask', 'silent', 'ambiguous')),
    basis       TEXT,
    user_action TEXT,
    at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now'))
);
INSERT INTO trigger_log__new (id, project, plan_id, node_key, path, rule_id, verdict, basis, user_action, at)
    SELECT id, project, plan_id, node_key, path, rule_id, verdict, basis, user_action, at FROM trigger_log;
DROP TRIGGER IF EXISTS trg_trigger_log_no_update;
DROP TRIGGER IF EXISTS trg_trigger_log_no_delete;
DROP TABLE trigger_log;
ALTER TABLE trigger_log__new RENAME TO trigger_log;
CREATE INDEX IF NOT EXISTS idx_trigger_log_plan ON trigger_log (project, plan_id);
CREATE TRIGGER IF NOT EXISTS trg_trigger_log_no_update BEFORE UPDATE ON trigger_log
BEGIN SELECT RAISE(ABORT, 'trigger_log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_trigger_log_no_delete BEFORE DELETE ON trigger_log
BEGIN SELECT RAISE(ABORT, 'trigger_log is append-only'); END;

-- ── Part 2 (Task 1): shown, adopted, headline, session, overhead ─────────────
-- read_hit: a record was taken out and put in front of the agent (plan /
-- edit / close / why). It says the system told; it never says anyone read.
CREATE TABLE IF NOT EXISTS read_hit (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    reason_id      INTEGER NOT NULL REFERENCES change_reason(id),
    project        TEXT NOT NULL,
    plan_id        TEXT,
    session_id     TEXT,
    step_id        TEXT,
    moment         TEXT NOT NULL CHECK (moment IN ('plan', 'edit', 'close', 'why')),
    injected_chars INTEGER,
    at             TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_read_hit_reason ON read_hit (reason_id, moment);
CREATE INDEX IF NOT EXISTS idx_read_hit_plan ON read_hit (plan_id, moment);
CREATE TRIGGER IF NOT EXISTS trg_read_hit_no_update BEFORE UPDATE ON read_hit
BEGIN SELECT RAISE(ABORT, 'read_hit is append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_read_hit_no_delete BEFORE DELETE ON read_hit
BEGIN SELECT RAISE(ABORT, 'read_hit is append-only'); END;

-- influence: a record was cited by id and shaped the current plan — through
-- a headline response, a reason's `because`, or an acknowledged constraint.
CREATE TABLE IF NOT EXISTS influence (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    reason_id  INTEGER NOT NULL REFERENCES change_reason(id),
    project    TEXT NOT NULL,
    plan_id    TEXT,
    session_id TEXT,
    step_id    TEXT,
    node_key   TEXT,
    via        TEXT NOT NULL CHECK (via IN ('headline_response', 'reason_because', 'constraint_ack')),
    by         TEXT NOT NULL CHECK (by IN ('agent', 'human')),
    at         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_influence_reason ON influence (reason_id);
CREATE INDEX IF NOT EXISTS idx_influence_plan ON influence (plan_id);
CREATE TRIGGER IF NOT EXISTS trg_influence_no_update BEFORE UPDATE ON influence
BEGIN SELECT RAISE(ABORT, 'influence is append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_influence_no_delete BEFORE DELETE ON influence
BEGIN SELECT RAISE(ABORT, 'influence is append-only'); END;

-- headline: what the two-layer check found for a plan (or a session in the
-- degraded mode); recomputing appends a new row, never rewrites.
CREATE TABLE IF NOT EXISTS headline (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    project       TEXT NOT NULL,
    plan_id       TEXT,
    session_id    TEXT,
    computed_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now')),
    findings_json TEXT NOT NULL,
    CHECK (plan_id IS NOT NULL OR session_id IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS idx_headline_plan ON headline (plan_id, id);
CREATE TRIGGER IF NOT EXISTS trg_headline_no_update BEFORE UPDATE ON headline
BEGIN SELECT RAISE(ABORT, 'headline is append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_headline_no_delete BEFORE DELETE ON headline
BEGIN SELECT RAISE(ABORT, 'headline is append-only'); END;

CREATE TABLE IF NOT EXISTS headline_response (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    headline_id INTEGER NOT NULL REFERENCES headline(id),
    finding_id  TEXT NOT NULL,
    action      TEXT NOT NULL CHECK (action IN ('revise', 'proceed')),
    rationale   TEXT,
    by          TEXT NOT NULL CHECK (by IN ('agent', 'human')),
    cites_json  TEXT,
    at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now')),
    UNIQUE (headline_id, finding_id)
);
CREATE TRIGGER IF NOT EXISTS trg_headline_response_no_update BEFORE UPDATE ON headline_response
BEGIN SELECT RAISE(ABORT, 'headline_response is append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_headline_response_no_delete BEFORE DELETE ON headline_response
BEGIN SELECT RAISE(ABORT, 'headline_response is append-only'); END;

-- session_run: the degraded mode (hooks only) — one row per Claude Code
-- session that ended in a registered repo; the Stop hook may queue a graph
-- refresh (trigger=session). The only mutable table of this migration.
CREATE TABLE IF NOT EXISTS session_run (
    session_id    TEXT PRIMARY KEY,
    project       TEXT,
    cwd           TEXT,
    started_at    TEXT,
    ended_at      TEXT,
    psg_run_id    INTEGER,
    refresh_state TEXT NOT NULL DEFAULT 'skipped' CHECK (refresh_state IN ('skipped', 'queued', 'done', 'failed')),
    note          TEXT
);

-- overhead (Task 6): the head of a Bash command, so provledger's own calls
-- can be told apart from the agent's; the latest headline on the plan row.
ALTER TABLE tool_call_log ADD COLUMN command_head TEXT;
ALTER TABLE Plans ADD COLUMN headline_json TEXT;

-- reason_stats_v: per record, how often it was shown at each moment and how
-- often it was adopted — never summed across moments (an edit-time hook can
-- show a hot file's constraint dozens of times a day).
CREATE VIEW IF NOT EXISTS reason_stats_v AS
SELECT r.id AS reason_id, r.project, r.node_key, r.tier, r.role,
       (SELECT COUNT(*) FROM read_hit h WHERE h.reason_id = r.id AND h.moment = 'plan')  AS shown_plan,
       (SELECT COUNT(*) FROM read_hit h WHERE h.reason_id = r.id AND h.moment = 'edit')  AS shown_edit,
       (SELECT COUNT(*) FROM read_hit h WHERE h.reason_id = r.id AND h.moment = 'close') AS shown_close,
       (SELECT COUNT(*) FROM read_hit h WHERE h.reason_id = r.id AND h.moment = 'why')   AS shown_why,
       (SELECT COUNT(*) FROM influence i WHERE i.reason_id = r.id)                        AS adopted
FROM change_reason r;

-- (full-text search lives outside this file: why.ensure_fts creates the FTS5
--  table lazily and falls back to LIKE where the build has no FTS5 — a failing
--  statement here would roll the whole migration back.)
