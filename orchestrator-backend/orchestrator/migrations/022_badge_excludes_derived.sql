-- 022_badge_excludes_derived.sql — a badge means someone said something.
--
-- On the dogfood repo, 1679 of 2847 nodes carried a badge and almost every one
-- of them was a close-time `derived` row written by a rule (R5 alone wrote the
-- same "covered by active constraint #N" sentence once per plan that met the
-- node). A mark that is on 59% of the graph tells a reader nothing, and the
-- Graph view's `story` mode — "the nodes that have a story" — selected 1601
-- nodes because of it.
--
-- So the badge counts what a PERSON or a MODEL put there: stated and asserted
-- reasons, rejected alternatives, active constraints. Derived rows are not
-- deleted and not hidden: they get their own column, next to `minor`, so the
-- page can still say how many rules fired on a node. They just no longer make
-- the node look like it has a story.
DROP VIEW IF EXISTS node_badge_v;
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
       -- counted and shown, never mistaken for a story
       SUM(CASE WHEN r.role = 'reason' AND r.tier = 'derived' THEN 1 ELSE 0 END) AS derived,
       SUM(CASE WHEN r.role = 'reason' AND COALESCE(r.significance_eff, 'major') = 'minor' THEN 1 ELSE 0 END) AS minor,
       MAX(r.occurred_at) AS last_at
FROM change_reason_v r
WHERE r.node_key IS NOT NULL
GROUP BY r.node_key, r.project;
