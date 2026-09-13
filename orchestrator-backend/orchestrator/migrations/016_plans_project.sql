-- Migration 016: Plans.project / Plans.project_source (FL-014).
-- Which project a plan belongs to is EXPLICIT: declared in the plan input, or
-- derived from the repo the plan was published from (cwd), or — for plans
-- published before this column existed — inferred once from goal text
-- ('legacy'). review_and_complete reads this column; the goal-text token match
-- is a compatibility path for NULL only. 'none' is a legal project value:
-- "this plan belongs to no registered project" — a decision, not an omission.
-- The attribution is assigned once (db.set_plan_project refuses to overwrite).
ALTER TABLE Plans ADD COLUMN project TEXT;
ALTER TABLE Plans ADD COLUMN project_source TEXT
    CHECK (project_source IS NULL OR project_source IN ('declared', 'cwd', 'legacy'));
CREATE INDEX IF NOT EXISTS idx_plans_project ON Plans (project, status);
