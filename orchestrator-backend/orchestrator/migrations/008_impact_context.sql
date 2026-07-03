-- Migration 008: per-plan impact_context (provLedger Phase D)
--
-- Adds a single nullable column to Plans:
--   impact_context — JSON blob produced by impact_preflight.compute_impact_context
--                    at publish time (the forward blast-radius analysis: targets,
--                    per-symbol callers/consumers/dtypes/lineage, upstream-data
--                    assumptions, ledger reminders, capability boundary).
--
-- Nullable so existing plans (and project-less plans) continue to work — they
-- simply carry NULL here. "Publish a plan" and "do impact analysis" become one
-- atomic act for plans that name a tracked project (enforced in publish_plan.py).

ALTER TABLE Plans ADD COLUMN impact_context TEXT;
