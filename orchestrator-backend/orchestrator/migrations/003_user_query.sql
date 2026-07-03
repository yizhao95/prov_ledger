-- Migration 003: per-plan user_query
--
-- Adds a single nullable column to Plans:
--   user_query — the original user prompt that triggered plan creation.
--                Distinct from `original_goal` (which is the AGENT's
--                summary). user_query is verbatim what the human typed.
--
-- Nullable so existing plans (pre-v3) continue to work — they'll fall
-- back to plan_id as the title on the history page.
--
-- Used by the dashboard history page as the human-friendly row title.

ALTER TABLE Plans ADD COLUMN user_query TEXT;
