-- 031_evidence_log.sql — A6: why this one is blank.
--
-- A reason with no checkable source can be blank for three different reasons,
-- and the difference is the entire value of the row. Nobody went looking (no
-- communication tool on this host, or the significance cap cut the slot off);
-- somebody looked in the plan's window and it held nothing; the look started and
-- the evidence pass ran out of time. Folded into a single "no evidence" they are
-- indistinguishable, and a degradation nobody can see is what this product
-- exists to prevent — so every slot's ending gets a row.
--
-- `searched` is redundant with `outcome` on purpose: two ways of saying the same
-- thing, with a CHECK that makes them agree, so a row written around the store
-- cannot claim it never searched and found nothing at the same time.
--
-- Append-only, with the trigger pair every log in this schema carries: a second
-- look at the same slot is another row beside the first, never a correction of
-- it. The evidence attached later does not retract the afternoon when nothing
-- could be found.
--
-- Nothing here is hash-chained: chains cover the provenance records that a
-- disagreement rests on (utterance / reference / change_reason / reference_check).
-- This is a log OF those records' making, like trigger_log and significance_log
-- beside it, and it is append-only for the same reason they are.
CREATE TABLE IF NOT EXISTS evidence_log (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id        TEXT NOT NULL,
    node_key       TEXT NOT NULL,
    -- the tier the slot landed at, and how checkable it ended up being; NULL when
    -- no reason row exists yet, which is itself a thing worth being able to see
    reason_tier    TEXT CHECK (reason_tier IS NULL OR reason_tier IN ('stated', 'asserted', 'derived', 'unstated')),
    evidence_level TEXT CHECK (evidence_level IS NULL OR evidence_level IN ('linked', 'verbal', 'task_context', 'unstated')),
    searched       INTEGER NOT NULL CHECK (searched IN (0, 1)),
    tool_hint      TEXT,                          -- which of the host's tools was used, in the host's own words
    outcome        TEXT NOT NULL CHECK (outcome IN ('attached', 'found_nothing', 'timed_out', 'not_searched')),
    elapsed_ms     INTEGER CHECK (elapsed_ms IS NULL OR elapsed_ms >= 0),
    at             TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now')),
    CHECK ((outcome = 'not_searched') = (searched = 0))
);
CREATE INDEX IF NOT EXISTS idx_evidence_log_plan ON evidence_log (plan_id, node_key, id);
CREATE TRIGGER IF NOT EXISTS trg_evidence_log_no_update BEFORE UPDATE ON evidence_log
BEGIN SELECT RAISE(ABORT, 'evidence_log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_evidence_log_no_delete BEFORE DELETE ON evidence_log
BEGIN SELECT RAISE(ABORT, 'evidence_log is append-only'); END;
