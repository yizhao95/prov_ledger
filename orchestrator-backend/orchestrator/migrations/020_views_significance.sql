-- 020_views_significance.sql — DP phase 2b (the whole DDL lands in Task 0 so the
-- dev DB is stamped once; Task 1 tests every piece — a migration file grown
-- across tasks would have to be re-stamped, see the dogfood log).
--
-- Part 1 · a plan carries the Claude Code session it was published from, so the
-- words said in that session BEFORE the plan existed are R0 candidates (FL-069).
ALTER TABLE Plans ADD COLUMN session_id TEXT;
CREATE INDEX IF NOT EXISTS idx_plans_session ON Plans (session_id);

-- Part 2 · significance: every judgement leaves a row — the system's hint with its
-- basis, an optional LLM verdict (runner named), a human mark. Append-only.
-- change_reason.significance (the stored column of phase 1) is no longer written;
-- change_reason_v exposes significance_eff = latest verdict → latest hint → NULL.
CREATE TABLE IF NOT EXISTS significance_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    reason_id     INTEGER NOT NULL REFERENCES change_reason(id),
    project       TEXT NOT NULL,
    hint          TEXT NOT NULL CHECK (hint IN ('major', 'minor')),
    hint_basis    TEXT NOT NULL,
    verdict       TEXT CHECK (verdict IN ('major', 'minor')),
    verdict_basis TEXT,
    judged_by     TEXT NOT NULL CHECK (judged_by IN ('hint', 'llm', 'human')),
    runner        TEXT,
    at            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now')),
    CHECK (judged_by = 'hint' OR verdict IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS idx_significance_log_reason ON significance_log (reason_id, id);
CREATE TRIGGER IF NOT EXISTS trg_significance_log_no_update BEFORE UPDATE ON significance_log
BEGIN SELECT RAISE(ABORT, 'significance_log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_significance_log_no_delete BEFORE DELETE ON significance_log
BEGIN SELECT RAISE(ABORT, 'significance_log is append-only'); END;

DROP VIEW IF EXISTS change_reason_v;
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
       ) AS significance_eff
FROM change_reason r;

-- Part 3 · node badges: per node_key, how many records have a story worth a badge —
-- reasons whose effective significance is not minor, rejected paths, active constraints.
CREATE VIEW IF NOT EXISTS node_badge_v AS
SELECT r.node_key,
       r.project,
       SUM(CASE WHEN r.role = 'reason' AND r.tier <> 'unstated' AND COALESCE(r.significance_eff, 'major') <> 'minor' THEN 1 ELSE 0 END) AS reasons,
       SUM(CASE WHEN r.role = 'rejected_path' THEN 1 ELSE 0 END) AS rejected_paths,
       SUM(CASE WHEN r.role = 'constraint' AND r.state = 'active' AND r.superseded_by IS NULL THEN 1 ELSE 0 END) AS constraints,
       SUM(CASE WHEN (r.role = 'reason' AND r.tier <> 'unstated' AND COALESCE(r.significance_eff, 'major') <> 'minor')
                  OR r.role = 'rejected_path'
                  OR (r.role = 'constraint' AND r.state = 'active' AND r.superseded_by IS NULL) THEN 1 ELSE 0 END) AS badge,
       SUM(CASE WHEN r.role = 'reason' AND COALESCE(r.significance_eff, 'major') = 'minor' THEN 1 ELSE 0 END) AS minor,
       MAX(r.occurred_at) AS last_at
FROM change_reason_v r
WHERE r.node_key IS NOT NULL
GROUP BY r.node_key, r.project;
