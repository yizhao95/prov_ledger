-- 032: tool_call_log.failed (FL-208). A failed tool call fires PostToolUseFailure,
-- never PostToolUse, so every row before this one is a call that succeeded.
ALTER TABLE tool_call_log ADD COLUMN failed INTEGER NOT NULL DEFAULT 0;
