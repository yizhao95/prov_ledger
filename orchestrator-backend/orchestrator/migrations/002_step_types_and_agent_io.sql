-- Migration 002: step_type + agent I/O capture
--
-- Adds three new nullable columns to Steps:
--   step_type      — one of THINKING/DOCUMENTATION/CODE/COMMAND/SUB_AGENT/ANALYSIS, or NULL for legacy
--   agent_input    — input prompt sent to a sub-agent (only set when step_type='SUB_AGENT')
--   agent_output   — output returned from a sub-agent (only set when step_type='SUB_AGENT')
--
-- All nullable so existing 100+ historical step rows continue to work unchanged.
-- New plans should populate step_type. agent_input/output are populated for SUB_AGENT steps only.

ALTER TABLE Steps ADD COLUMN step_type TEXT
    CHECK (step_type IS NULL OR step_type IN
        ('THINKING','DOCUMENTATION','CODE','COMMAND','SUB_AGENT','ANALYSIS'));

ALTER TABLE Steps ADD COLUMN agent_input  TEXT;
ALTER TABLE Steps ADD COLUMN agent_output TEXT;

-- Index for filtering by type (useful for dashboard analytics)
CREATE INDEX IF NOT EXISTS idx_steps_type ON Steps(step_type);
