-- Migration 018: decision provenance (DP phase 0 + 1).
--
-- Phase 0 (Task 0): tool_call_log — one row per tool call, written by the
-- PostToolUse hook (orchestrator.hooks). plan_metrics attributes rows to a
-- plan by its created_at..completed_at window and the project's repo (cwd),
-- so every phase can answer "which tool call did this cost" (north star:
-- the tool must not silently get slower). Append-only like node_reason.
-- Phase 1 (Task 1) adds utterance / reference / change_reason /
-- reference_link / trigger_log / migration_state, the views and the
-- UPDATE whitelist triggers to this same file.
CREATE TABLE IF NOT EXISTS tool_call_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL,
    cwd         TEXT,
    tool_name   TEXT NOT NULL,
    at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_tool_call_log_at ON tool_call_log (at);
CREATE TRIGGER IF NOT EXISTS trg_tool_call_log_no_update BEFORE UPDATE ON tool_call_log
BEGIN SELECT RAISE(ABORT, 'tool_call_log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_tool_call_log_no_delete BEFORE DELETE ON tool_call_log
BEGIN SELECT RAISE(ABORT, 'tool_call_log is append-only'); END;

-- ── Phase 1 (Task 1): the decision-provenance tables ─────────────────────────
-- utterance: the user's words, verbatim, written only by the UserPromptSubmit
-- hook and `provledger note`. Never updated, never deleted.
CREATE TABLE IF NOT EXISTS utterance (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL,
    project     TEXT,
    plan_id     TEXT,
    text        TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    recorded_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now')),
    visibility  TEXT NOT NULL DEFAULT 'personal' CHECK (visibility IN ('personal', 'shareable')),
    prev_hash   TEXT,
    hash        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_utterance_plan ON utterance (plan_id, id);
CREATE INDEX IF NOT EXISTS idx_utterance_project_at ON utterance (project, occurred_at);
CREATE TRIGGER IF NOT EXISTS trg_utterance_no_update BEFORE UPDATE ON utterance
BEGIN SELECT RAISE(ABORT, 'utterance is append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_utterance_no_delete BEFORE DELETE ON utterance
BEGIN SELECT RAISE(ABORT, 'utterance is append-only'); END;

-- reference: a pointer to where a decision came from (email, meeting, chat,
-- ticket, doc, commit, a verbal exchange). Never the body — only a label
-- (≤ 512 chars) and an optional uri. verifiability: linked (has a uri),
-- verbal (kind=verbal, no uri by definition), unreachable (no uri yet).
CREATE TABLE IF NOT EXISTS reference (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    project       TEXT NOT NULL,
    kind          TEXT NOT NULL CHECK (kind IN ('email', 'meeting', 'chat', 'ticket', 'doc', 'commit', 'verbal', 'other')),
    uri           TEXT,
    label         TEXT NOT NULL CHECK (length(label) <= 512),
    occurred_at   TEXT NOT NULL,
    registered_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now')),
    verifiability TEXT NOT NULL CHECK (verifiability IN ('linked', 'verbal', 'unreachable')),
    visibility    TEXT NOT NULL DEFAULT 'shareable' CHECK (visibility IN ('personal', 'shareable')),
    last_checked  TEXT,
    prev_hash     TEXT,
    hash          TEXT NOT NULL,
    CHECK (kind <> 'verbal' OR uri IS NULL),
    CHECK (kind <> 'verbal' OR verifiability = 'verbal'),
    CHECK (kind = 'verbal' OR verifiability <> 'verbal'),
    CHECK (verifiability <> 'linked' OR uri IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS idx_reference_project ON reference (project, kind);
CREATE TRIGGER IF NOT EXISTS trg_reference_update_whitelist BEFORE UPDATE ON reference
WHEN NEW.id IS NOT OLD.id OR NEW.project IS NOT OLD.project OR NEW.kind IS NOT OLD.kind OR NEW.uri IS NOT OLD.uri
  OR NEW.label IS NOT OLD.label OR NEW.occurred_at IS NOT OLD.occurred_at OR NEW.registered_at IS NOT OLD.registered_at
  OR NEW.verifiability IS NOT OLD.verifiability OR NEW.visibility IS NOT OLD.visibility
  OR NEW.prev_hash IS NOT OLD.prev_hash OR NEW.hash IS NOT OLD.hash
BEGIN SELECT RAISE(ABORT, 'reference: only last_checked may change'); END;
CREATE TRIGGER IF NOT EXISTS trg_reference_no_delete BEFORE DELETE ON reference
BEGIN SELECT RAISE(ABORT, 'reference is append-only'); END;

-- change_reason: WHY a node changed, with its source level decided by what
-- the row points at — stated ⇔ a verbatim utterance span; asserted ⇒ an
-- interpretation; derived ⇒ a rule id; unstated ⇒ nothing but the anchor.
CREATE TABLE IF NOT EXISTS change_reason (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    project               TEXT NOT NULL,
    node_key              TEXT,
    run_id                INTEGER,
    event_id              INTEGER,
    plan_id               TEXT NOT NULL,
    step_id               TEXT,
    kind                  TEXT NOT NULL CHECK (kind IN ('technical', 'organizational', 'mixed')),
    role                  TEXT NOT NULL DEFAULT 'reason' CHECK (role IN ('reason', 'rejected_path', 'constraint')),
    verbatim_utterance_id INTEGER REFERENCES utterance(id),
    verbatim_start        INTEGER,
    verbatim_end          INTEGER,
    interpretation        TEXT,
    statement             TEXT,
    rationale             TEXT,
    rationale_visibility  TEXT NOT NULL DEFAULT 'personal' CHECK (rationale_visibility IN ('personal', 'shareable')),
    statement_visibility  TEXT NOT NULL DEFAULT 'shareable' CHECK (statement_visibility IN ('personal', 'shareable')),
    state                 TEXT NOT NULL DEFAULT 'active' CHECK (state IN ('active', 'superseded', 'expired', 'unknown')),
    superseded_by         INTEGER,
    review_after          TEXT,
    occurred_at           TEXT NOT NULL,
    recorded_at           TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now')),
    recorded_by           TEXT NOT NULL CHECK (recorded_by IN ('human', 'agent', 'system')),
    tier                  TEXT NOT NULL CHECK (tier IN ('stated', 'asserted', 'derived', 'unstated')),
    rule_id               TEXT,
    significance          TEXT CHECK (significance IN ('major', 'minor')),
    prev_hash             TEXT,
    hash                  TEXT NOT NULL,
    CHECK ((tier = 'stated') = (verbatim_utterance_id IS NOT NULL)),
    CHECK (verbatim_utterance_id IS NULL OR (verbatim_start IS NOT NULL AND verbatim_end IS NOT NULL AND verbatim_start >= 0 AND verbatim_end > verbatim_start)),
    CHECK (tier <> 'asserted' OR interpretation IS NOT NULL OR statement IS NOT NULL),
    CHECK (tier <> 'derived' OR rule_id IS NOT NULL),
    CHECK (tier <> 'unstated' OR (interpretation IS NULL AND statement IS NULL AND rationale IS NULL AND verbatim_utterance_id IS NULL)),
    CHECK (node_key IS NOT NULL OR role = 'rejected_path')
);
CREATE INDEX IF NOT EXISTS idx_change_reason_node ON change_reason (node_key, id);
CREATE INDEX IF NOT EXISTS idx_change_reason_plan ON change_reason (project, plan_id);
CREATE TRIGGER IF NOT EXISTS trg_change_reason_update_whitelist BEFORE UPDATE ON change_reason
WHEN NEW.id IS NOT OLD.id OR NEW.project IS NOT OLD.project OR NEW.node_key IS NOT OLD.node_key OR NEW.run_id IS NOT OLD.run_id
  OR NEW.event_id IS NOT OLD.event_id OR NEW.plan_id IS NOT OLD.plan_id OR NEW.step_id IS NOT OLD.step_id
  OR NEW.kind IS NOT OLD.kind OR NEW.role IS NOT OLD.role OR NEW.verbatim_utterance_id IS NOT OLD.verbatim_utterance_id
  OR NEW.verbatim_start IS NOT OLD.verbatim_start OR NEW.verbatim_end IS NOT OLD.verbatim_end
  OR NEW.interpretation IS NOT OLD.interpretation OR NEW.statement IS NOT OLD.statement OR NEW.rationale IS NOT OLD.rationale
  OR NEW.rationale_visibility IS NOT OLD.rationale_visibility OR NEW.statement_visibility IS NOT OLD.statement_visibility
  OR NEW.state IS NOT OLD.state OR NEW.review_after IS NOT OLD.review_after
  OR NEW.occurred_at IS NOT OLD.occurred_at OR NEW.recorded_at IS NOT OLD.recorded_at OR NEW.recorded_by IS NOT OLD.recorded_by
  OR NEW.tier IS NOT OLD.tier OR NEW.rule_id IS NOT OLD.rule_id OR NEW.significance IS NOT OLD.significance
  OR NEW.prev_hash IS NOT OLD.prev_hash OR NEW.hash IS NOT OLD.hash
BEGIN SELECT RAISE(ABORT, 'change_reason: only superseded_by may change'); END;
CREATE TRIGGER IF NOT EXISTS trg_change_reason_no_delete BEFORE DELETE ON change_reason
BEGIN SELECT RAISE(ABORT, 'change_reason is append-only'); END;

CREATE TABLE IF NOT EXISTS reference_link (
    reason_id    INTEGER NOT NULL REFERENCES change_reason(id),
    reference_id INTEGER NOT NULL REFERENCES reference(id),
    PRIMARY KEY (reason_id, reference_id)
);
CREATE TRIGGER IF NOT EXISTS trg_reference_link_no_update BEFORE UPDATE ON reference_link
BEGIN SELECT RAISE(ABORT, 'reference_link is append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_reference_link_no_delete BEFORE DELETE ON reference_link
BEGIN SELECT RAISE(ABORT, 'reference_link is append-only'); END;

-- trigger_log: every verdict of the reason rules (auto / ask / silent), so
-- the false-trigger and miss rates can be computed later — never silent.
CREATE TABLE IF NOT EXISTS trigger_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project     TEXT NOT NULL,
    plan_id     TEXT NOT NULL,
    node_key    TEXT,
    path        TEXT NOT NULL CHECK (path IN ('code', 'external')),
    rule_id     TEXT,
    verdict     TEXT NOT NULL CHECK (verdict IN ('auto', 'ask', 'silent')),
    basis       TEXT,
    user_action TEXT,
    at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_trigger_log_plan ON trigger_log (project, plan_id);
CREATE TRIGGER IF NOT EXISTS trg_trigger_log_no_update BEFORE UPDATE ON trigger_log
BEGIN SELECT RAISE(ABORT, 'trigger_log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_trigger_log_no_delete BEFORE DELETE ON trigger_log
BEGIN SELECT RAISE(ABORT, 'trigger_log is append-only'); END;

CREATE TABLE IF NOT EXISTS migration_state (
    key   TEXT PRIMARY KEY,
    value TEXT,
    at    TEXT
);

-- evidence_level (shown as "source level") is COMPUTED, never stored:
-- linked > verbal > task_context > unstated.
CREATE VIEW IF NOT EXISTS change_reason_v AS
SELECT r.*,
       CASE
         WHEN EXISTS (SELECT 1 FROM reference_link l JOIN reference f ON f.id = l.reference_id
                      WHERE l.reason_id = r.id AND f.verifiability = 'linked') THEN 'linked'
         WHEN r.verbatim_utterance_id IS NOT NULL
              OR EXISTS (SELECT 1 FROM reference_link l JOIN reference f ON f.id = l.reference_id
                         WHERE l.reason_id = r.id AND f.kind = 'verbal') THEN 'verbal'
         WHEN r.tier = 'unstated' THEN 'unstated'
         ELSE 'task_context'
       END AS evidence_level
FROM change_reason r;

-- node_reason_v: the old column names over the new table, one release long.
CREATE VIEW IF NOT EXISTS node_reason_v AS
SELECT id, node_key, project, run_id, plan_id, step_id,
       CASE role WHEN 'constraint' THEN 'constraint_ref' ELSE role END AS kind,
       COALESCE(interpretation, statement) AS text,
       recorded_by AS source,
       CASE tier WHEN 'unstated' THEN 'derived' ELSE tier END AS tier,
       recorded_at AS created_at
FROM change_reason;
