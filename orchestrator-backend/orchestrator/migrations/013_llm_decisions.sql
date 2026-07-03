-- Migration 013: llm_decisions — every LLM data decision, especially failures.
--
-- The "never repeat a mistake" core: each row records what the LLM decided about a
-- data drift (observed before/after, decision, rationale, action, outcome) plus any
-- natural-language human feedback that shaped it. Written by the backbone flow only;
-- humans never write directly. Auto-synced to LedgerEntries so the next plan's fuzzy
-- match surfaces the prior decision. `project` is the instance discriminator.

CREATE TABLE IF NOT EXISTS llm_decisions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    project         TEXT,
    plan_id         TEXT,
    step_id         TEXT,
    dataset         TEXT,
    column_name     TEXT,
    observed_before TEXT,
    observed_after  TEXT,
    drift_kind      TEXT,
    decision        TEXT NOT NULL,
    rationale       TEXT,
    action          TEXT,
    outcome         TEXT,
    human_feedback  TEXT,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_llm_decisions_ds
    ON llm_decisions (project, dataset, column_name);
