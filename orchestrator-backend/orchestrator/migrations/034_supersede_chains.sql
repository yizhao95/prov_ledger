-- 034_supersede_chains.sql — a supersede chain reads as its newest row (FL-238, FL-239).
--
-- provenance.supersede(old, new) is the one UPDATE change_reason allows: the old
-- row keeps its words and points at its successor. Constraint readers skipped a
-- superseded row; reason and rejected-path readers printed both, so appending a
-- correction to a false `stated` row left the false row on screen. Readers now
-- show the newest row and say which rows it corrects:
--
--   * change_reason_v: 030's body plus `supersedes`, the ids of the rows this one
--     supersedes. Every reader gets it from the view; none computes it again.
--   * node_badge_v: 030's body (022's counts) with superseded rows left out, so a
--     corrected row and its correction count once. Constraints already did so.
--   * an index on change_reason(superseded_by) for the lookup above.

CREATE INDEX IF NOT EXISTS idx_change_reason_superseded_by ON change_reason (superseded_by);

DROP VIEW IF EXISTS node_badge_v;
DROP VIEW IF EXISTS change_reason_v;

-- 030's body, plus supersedes.
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

-- 030's body (022's counts), superseded rows left out.
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
