-- 036_task_reasons.sql — a reason may belong to the task (task-level redesign, step 3a).
--
-- 018 created change_reason with CHECK (node_key IS NOT NULL OR role =
-- 'rejected_path'): every reason had to hang on a node. A task's reason belongs to
-- the plan; the nodes the task changed reach it through the state graph's
-- history (their runs carry the plan id). A constraint still names its nodes.
--
-- SQLite cannot loosen a CHECK in place, so the table is rebuilt the way 033
-- rebuilt Plans: the four views that read it come down first (a rename re-parses
-- every view), the old table is renamed with legacy_alter_table on so the four
-- tables that point at change_reason (reference_link, read_hit, influence,
-- significance_log) keep pointing at the name, every row is copied with its id
-- and hash (the chain hashes a row's values, not the table, so it still
-- verifies), the old table goes (and with it its indexes and triggers), and the
-- indexes, triggers and views come back with their latest bodies, unchanged.
--
-- Two append-only tables come with it:
--   reason_node   which task reason a changed node belongs to, written only when
--                 a plan did several things (one task covers every node)
--   plan_bundle   one sentence on why those things share a plan

PRAGMA foreign_keys = OFF;
PRAGMA legacy_alter_table = ON;

DROP VIEW IF EXISTS node_badge_v;
DROP VIEW IF EXISTS change_reason_v;
DROP VIEW IF EXISTS node_reason_v;
DROP VIEW IF EXISTS reason_stats_v;

ALTER TABLE change_reason RENAME TO change_reason_old_036;

CREATE TABLE change_reason (
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
    CHECK (node_key IS NOT NULL OR role IN ('rejected_path', 'reason'))
);

INSERT INTO change_reason (id, project, node_key, run_id, event_id, plan_id, step_id, kind, role, verbatim_utterance_id, verbatim_start, verbatim_end, interpretation, statement, rationale, rationale_visibility, statement_visibility, state, superseded_by, review_after, occurred_at, recorded_at, recorded_by, tier, rule_id, significance, prev_hash, hash)
SELECT id, project, node_key, run_id, event_id, plan_id, step_id, kind, role, verbatim_utterance_id, verbatim_start, verbatim_end, interpretation, statement, rationale, rationale_visibility, statement_visibility, state, superseded_by, review_after, occurred_at, recorded_at, recorded_by, tier, rule_id, significance, prev_hash, hash FROM change_reason_old_036 ORDER BY id;

DROP TABLE change_reason_old_036;

-- the indexes, triggers and views as they stood after 035, unchanged
CREATE INDEX idx_change_reason_node ON change_reason (node_key, id);

CREATE INDEX idx_change_reason_plan ON change_reason (project, plan_id);

CREATE INDEX idx_change_reason_superseded_by ON change_reason (superseded_by);

CREATE TRIGGER trg_change_reason_update_whitelist BEFORE UPDATE ON change_reason
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

CREATE TRIGGER trg_change_reason_no_delete BEFORE DELETE ON change_reason
BEGIN SELECT RAISE(ABORT, 'change_reason is append-only'); END;

CREATE VIEW change_reason_v AS
SELECT r.*,
       CASE
         WHEN EXISTS (SELECT 1 FROM reference_link l JOIN reference f ON f.id = l.reference_id
                      WHERE l.reason_id = r.id AND f.verifiability = 'linked') THEN 'linked'
         WHEN r.verbatim_utterance_id IS NOT NULL
              OR EXISTS (SELECT 1 FROM reference_link l JOIN reference f ON f.id = l.reference_id
                         WHERE l.reason_id = r.id AND f.kind = 'verbal') THEN 'verbal'
         WHEN r.tier = 'unstated' THEN 'unstated'
         ELSE 'task_context'
       END AS evidence_level,
       COALESCE(
         (SELECT s.verdict FROM significance_log s WHERE s.reason_id = r.id AND s.verdict IS NOT NULL ORDER BY s.id DESC LIMIT 1),
         (SELECT s.hint FROM significance_log s WHERE s.reason_id = r.id ORDER BY s.id DESC LIMIT 1)
       ) AS significance_eff,
       -- which door the quoted words came in through; NULL unless the row is `stated`
       (SELECT u.origin FROM utterance u WHERE u.id = r.verbatim_utterance_id) AS utterance_origin,
       -- the ids of the rows this one supersedes ('12,15'); NULL when it corrects none.
       -- group_concat's order is not guaranteed: provenance.supersedes_ids sorts them.
       (SELECT group_concat(o.id) FROM change_reason o WHERE o.superseded_by = r.id) AS supersedes
FROM change_reason r;

CREATE VIEW node_badge_v AS
SELECT r.node_key,
       r.project,
       SUM(CASE WHEN r.role = 'reason' AND r.tier IN ('stated', 'asserted')
                 AND COALESCE(r.significance_eff, 'major') <> 'minor' THEN 1 ELSE 0 END) AS reasons,
       SUM(CASE WHEN r.role = 'rejected_path' THEN 1 ELSE 0 END) AS rejected_paths,
       SUM(CASE WHEN r.role = 'constraint' AND r.state = 'active' AND r.superseded_by IS NULL THEN 1 ELSE 0 END) AS constraints,
       SUM(CASE WHEN (r.role = 'reason' AND r.tier IN ('stated', 'asserted')
                       AND COALESCE(r.significance_eff, 'major') <> 'minor')
                  OR r.role = 'rejected_path'
                  OR (r.role = 'constraint' AND r.state = 'active' AND r.superseded_by IS NULL) THEN 1 ELSE 0 END) AS badge,
       SUM(CASE WHEN r.role = 'reason' AND r.tier = 'derived' THEN 1 ELSE 0 END) AS derived,
       SUM(CASE WHEN r.role = 'reason' AND COALESCE(r.significance_eff, 'major') = 'minor' THEN 1 ELSE 0 END) AS minor,
       MAX(r.occurred_at) AS last_at
FROM change_reason_v r
WHERE r.node_key IS NOT NULL
  AND r.superseded_by IS NULL
GROUP BY r.node_key, r.project;

CREATE VIEW node_reason_v AS
SELECT id, node_key, project, run_id, plan_id, step_id,
       CASE role WHEN 'constraint' THEN 'constraint_ref' ELSE role END AS kind,
       COALESCE(interpretation, statement) AS text,
       recorded_by AS source,
       CASE tier WHEN 'unstated' THEN 'derived' ELSE tier END AS tier,
       recorded_at AS created_at
FROM change_reason;

CREATE VIEW reason_stats_v AS
SELECT r.id AS reason_id, r.project, r.node_key, r.tier, r.role,
       (SELECT COUNT(*) FROM read_hit h WHERE h.reason_id = r.id AND h.moment = 'plan')  AS shown_plan,
       (SELECT COUNT(*) FROM read_hit h WHERE h.reason_id = r.id AND h.moment = 'edit')  AS shown_edit,
       (SELECT COUNT(*) FROM read_hit h WHERE h.reason_id = r.id AND h.moment = 'close') AS shown_close,
       (SELECT COUNT(*) FROM read_hit h WHERE h.reason_id = r.id AND h.moment = 'why')   AS shown_why,
       (SELECT COUNT(*) FROM influence i WHERE i.reason_id = r.id)                        AS adopted
FROM change_reason r;

CREATE TABLE IF NOT EXISTS reason_node (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    reason_id  INTEGER NOT NULL REFERENCES change_reason(id),
    node_key   TEXT NOT NULL,
    at         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now')),
    UNIQUE (reason_id, node_key)
);
CREATE INDEX IF NOT EXISTS idx_reason_node_node ON reason_node (node_key, reason_id);
CREATE TRIGGER IF NOT EXISTS trg_reason_node_no_update BEFORE UPDATE ON reason_node
BEGIN SELECT RAISE(ABORT, 'reason_node is append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_reason_node_no_delete BEFORE DELETE ON reason_node
BEGIN SELECT RAISE(ABORT, 'reason_node is append-only'); END;

CREATE TABLE IF NOT EXISTS plan_bundle (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id     TEXT NOT NULL,
    basis       TEXT NOT NULL,
    recorded_by TEXT NOT NULL CHECK (recorded_by IN ('agent', 'human', 'system')),
    at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_plan_bundle_plan ON plan_bundle (plan_id, id);
CREATE TRIGGER IF NOT EXISTS trg_plan_bundle_no_update BEFORE UPDATE ON plan_bundle
BEGIN SELECT RAISE(ABORT, 'plan_bundle is append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_plan_bundle_no_delete BEFORE DELETE ON plan_bundle
BEGIN SELECT RAISE(ABORT, 'plan_bundle is append-only'); END;

PRAGMA legacy_alter_table = OFF;
PRAGMA foreign_keys = ON;
