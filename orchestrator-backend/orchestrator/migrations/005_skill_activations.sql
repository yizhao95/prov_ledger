-- Migration 005: SkillActivations table
--
-- Records WHICH skills were activated for each plan + (optionally) which step.
-- Driven by the user request "include all the skills we activated/needed for
-- each user query, and write to sqlite database."
--
-- One row per activation event. Nullable step_id distinguishes:
--   step_id IS NULL  → activated at plan-init time (e.g., writing-plans iron-law)
--   step_id IS NOT NULL → activated mid-execution under a specific step
--                         (e.g., systematic-debugging triggered by an error in step F)
--
-- The `source` enum tags WHY the skill was loaded:
--   auto-search      — proactive skill search before any action
--   explicit-mention — user mentioned the skill name in their prompt
--   iron-law         — mandatory trigger (TDD on every coding task,
--                      writing-plans on every new task)
--   deferred-load    — loaded later when the task evolved (e.g., a sub-agent
--                      step needed a new skill the planner didn't anticipate)
--
-- The `reason` column is free-form prose for context the enum can't capture
-- (e.g., "user said 'write my weekly report'", "TDD trigger: 'fix this bug'").
--
-- Both source and reason are populated where possible — the enum powers
-- analytics ("how often is each source used?"), the reason aids audit.

CREATE TABLE SkillActivations (
    activation_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id          TEXT NOT NULL,
    step_id          TEXT,
    skill_name       TEXT NOT NULL,
    source           TEXT NOT NULL
                     CHECK (source IN (
                         'auto-search',
                         'explicit-mention',
                         'iron-law',
                         'deferred-load'
                     )),
    reason           TEXT,
    activated_at     TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (plan_id) REFERENCES Plans(plan_id),
    FOREIGN KEY (step_id) REFERENCES Steps(step_id)
);

CREATE INDEX idx_skill_activations_plan  ON SkillActivations(plan_id);
CREATE INDEX idx_skill_activations_skill ON SkillActivations(skill_name);
