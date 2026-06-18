-- Migration 006: Auto-review-and-complete step marker
--
-- Adds an is_review flag to Steps so the deterministic review_and_complete
-- procedure (api.review_and_complete) can find the single auto-appended
-- terminal step per plan in O(log n) and decide:
--   - all non-review steps COMPLETED → review step COMPLETED + plan COMPLETED
--   - any non-review step FAILED & UNRECOVERED (rest terminal) → review step FAILED + plan FAILED
--   - any non-review step FAILED but RECOVERED via deviation sub-tree → review step COMPLETED + plan COMPLETED
--   - any step still PENDING/IN_PROGRESS → no-op (wait)
--
-- Recovery semantics added Jun 2026 (post-006, no schema change): a FAILED
-- step counts as recovered when every direct non-review child is itself
-- recovered (recursive). See api._is_step_recovered for the walk.
--
-- Driven by the user request: "lets append another step, called review and
-- complete: this task will be deterministic flow, to check if all the previous
-- step is in completed status, if so, mark this task as completed. Note, this
-- process will be triggered by a script, not depending on LLM."
--
-- DEFAULT 0 means every existing pre-2026-05-26 Steps row keeps working as
-- a non-review (regular) step — additive migration, zero-risk for legacy plans.
-- New plans published via publish-plan.sh will get one extra row with is_review=1.

ALTER TABLE Steps ADD COLUMN is_review INTEGER NOT NULL DEFAULT 0;

-- Composite index supports the hot path inside api.review_and_complete:
--   "find the review step for plan X" (WHERE plan_id=? AND is_review=1)
-- and the tally query:
--   "tally non-review step statuses for plan X" (WHERE plan_id=? AND is_review=0)
CREATE INDEX IF NOT EXISTS idx_steps_plan_review ON Steps (plan_id, is_review);
