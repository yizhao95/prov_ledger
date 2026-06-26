# P2 — Optimization Implementation Plan (Bundles A + B + C)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans / subagent-driven-development. Steps use checkbox (`- [ ]`). Each finding ID maps to `docs/superpowers/specs/2026-06-25-audit-findings.md`.

**Goal:** Implement the maintainer-selected audit findings — Bundle A (reliability/bug fixes), Bundle B (data model + migration 010), Bundle C (dashboard UX) — under the migration-safe + green-suite guardrails.

**Architecture:** Five sequential chunks ordered by dependency. Chunk 1 (migration 010 + data model) is the linchpin: it is **all-additive** (ALTER ADD COLUMN, CREATE INDEX/TRIGGER/TABLE — no Steps rebuild), made safe by the migration runner that applies each file exactly once (`db.run_migrations` tracks `schema_version.migration_file`). Later chunks build on the new columns.

**Tech Stack:** Python stdlib (sqlite3), SQL migrations, pytest; FastAPI/HTMX/Jinja2 for the dashboard.

## Global Constraints

- **Migration-safe:** migrations 001–009 are immutable; all schema change ships in new `010_*.sql` (and beyond), applied once by the runner. No table rebuild — use ALTER/INDEX/TRIGGER.
- **Green suite is the gate:** baseline 462 passed / 3 skipped (+15 packaging) must stay green; run suites **per-suite, never combined** (see [[provledger-test-suites]]).
- **Every new column must be populated/read by real code** (no dangling columns).
- **TDD:** failing test → fix → green → commit, one finding (or tight cluster) per commit.
- **Unified venv:** `~/skill-workspace/.venv/bin/python` for all test runs.

---

## CHUNK 1 — migration 010 + backend/ledger data model

Findings: BE-D1, BE-D2, BE-D3, BE-D4, BE-C2, BE-C3, BE-C1, SK-D1.

### Task 1.1: migration 010 schema (additive)

**Files:**
- Create: `orchestrator-backend/migrations/010_data_model.sql`
- Test: `orchestrator-backend/tests/test_migration_010.py`

- [ ] **Step 1: Failing test** — assert, on a migrated DB: `Steps` has columns `failure_reason`, `attempt_count`, `updated_at`; `Plans` has `updated_at`, `review_state`; index `idx_steps_type` exists; a `Deviations` table exists with the expected columns; `LedgerEntries` has `superseded_by`, `updated_at`, `plan_id`, `hit_count`, `last_matched_at`; inserting a Step with `step_type='bogus'` raises (trigger enforces the enum); a valid `step_type` and NULL are accepted; the migration is idempotent (run_migrations twice → no error).

```python
# orchestrator-backend/tests/test_migration_010.py (key assertions)
def cols(conn, t): return {r[1] for r in conn.execute(f"PRAGMA table_info({t})")}
def test_010_adds_columns(migrated):       # migrated = fixture running run_migrations
    assert {"failure_reason","attempt_count","updated_at"} <= cols(migrated,"Steps")
    assert {"updated_at","review_state"} <= cols(migrated,"Plans")
    assert {"superseded_by","updated_at","plan_id","hit_count","last_matched_at"} <= cols(migrated,"LedgerEntries")
    assert cols(migrated,"Deviations") >= {"deviation_id","plan_id","target_step_id","justification","revision_count","created_at"}
def test_010_step_type_trigger(migrated): # insert plan+step with bad type -> IntegrityError/OperationalError
    ...
def test_010_idempotent(migrated): run_migrations(migrated); run_migrations(migrated)
```

- [ ] **Step 2: Run → fail** (`pytest orchestrator-backend/tests/test_migration_010.py`).

- [ ] **Step 3: Write `010_data_model.sql`**

```sql
-- Migration 010: data-model enrichment (additive only; no table rebuild).
-- Restores step_type enforcement (via trigger) + idx_steps_type dropped by 007,
-- adds lifecycle/audit columns, a Deviations history table, and ledger provenance.

-- BE-D3: first-class failure reason + retry count on Steps.
ALTER TABLE Steps ADD COLUMN failure_reason TEXT;
ALTER TABLE Steps ADD COLUMN attempt_count INTEGER NOT NULL DEFAULT 0;
-- BE-D2: lifecycle timestamps.
ALTER TABLE Steps ADD COLUMN updated_at TEXT;
ALTER TABLE Plans ADD COLUMN updated_at TEXT;
-- BE-D4: park-for-review state, distinct from IN_PROGRESS.
ALTER TABLE Plans ADD COLUMN review_state TEXT
    CHECK (review_state IS NULL OR review_state IN ('awaiting_agent','reviewed'));

-- BE-C3: restore the index 007 dropped.
CREATE INDEX IF NOT EXISTS idx_steps_type ON Steps(step_type);

-- BE-C2: restore DB-level step_type enforcement that 007 lost, via triggers
-- (avoids a risky table rebuild). Mirrors db.VALID_STEP_TYPES.
CREATE TRIGGER IF NOT EXISTS trg_steps_type_insert
BEFORE INSERT ON Steps
WHEN NEW.step_type IS NOT NULL AND NEW.step_type NOT IN
    ('THINKING','ANALYSIS','CODE','COMMAND','DOCUMENTATION','SUB_AGENT')
BEGIN SELECT RAISE(ABORT, 'invalid step_type'); END;

CREATE TRIGGER IF NOT EXISTS trg_steps_type_update
BEFORE UPDATE OF step_type ON Steps
WHEN NEW.step_type IS NOT NULL AND NEW.step_type NOT IN
    ('THINKING','ANALYSIS','CODE','COMMAND','DOCUMENTATION','SUB_AGENT')
BEGIN SELECT RAISE(ABORT, 'invalid step_type'); END;

-- BE-D1: deviation/revision history (the "why a plan changed" audit trail).
CREATE TABLE IF NOT EXISTS Deviations (
    deviation_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id          TEXT NOT NULL,
    target_step_id   TEXT,
    justification    TEXT NOT NULL,
    new_step_ids     TEXT,                 -- JSON list of inserted sub-step ids
    revision_count   INTEGER,
    created_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S','now')),
    FOREIGN KEY (plan_id) REFERENCES Plans(plan_id),
    FOREIGN KEY (target_step_id) REFERENCES Steps(step_id)
);
CREATE INDEX IF NOT EXISTS idx_deviations_plan ON Deviations(plan_id);

-- SK-D1: ledger provenance + relevance-ranking columns.
ALTER TABLE LedgerEntries ADD COLUMN superseded_by INTEGER;
ALTER TABLE LedgerEntries ADD COLUMN updated_at TEXT;
ALTER TABLE LedgerEntries ADD COLUMN plan_id TEXT;
ALTER TABLE LedgerEntries ADD COLUMN hit_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE LedgerEntries ADD COLUMN last_matched_at TEXT;
```

- [ ] **Step 4: Run → pass.** **Step 5: Commit** (`feat(db): migration 010 — data-model enrichment + restore step_type enforcement`).

### Task 1.2: persist deviation justification (BE-C1)

**Files:** Modify `orchestrator/db.py` (add `insert_deviation(conn, plan_id, target_step_id, justification, new_step_ids, revision_count)`), `orchestrator/api.py:141-160` (`evaluate_and_update_plan` calls it inside the same flow; return the real row), `tests/test_api.py`.

- [ ] Failing test: after `evaluate_and_update_plan(...)`, a `Deviations` row exists with the justification and the inserted sub-step ids. → implement `insert_deviation` + call it → green → commit.

### Task 1.3: populate failure_reason / updated_at / review_state in the write paths

**Files:** Modify `orchestrator/db.py` (`fail_step` also sets `failure_reason`; every status write sets `updated_at = _now()`; `set_completed` uses `completed_at = COALESCE(completed_at, ?)`), `api.py` `_open_agent_review` sets `Plans.review_state='awaiting_agent'`, `review_and_complete` clears/sets `reviewed`. Tests in `tests/test_api.py` / `tests/test_db.py`.

- [ ] Failing tests (failure_reason persisted on fail_step; updated_at set on transitions; review_state set when parked) → implement → green → commit.

### Task 1.4: ledger provenance wiring (SK-D1)

**Files:** Modify `skills/writing-plans/scripts/ledger_store.py` (`supersede_entry` sets `superseded_by` + `updated_at`; `add_entry` accepts optional `plan_id`; a `record_hit(entry_id)` bumps `hit_count`/`last_matched_at`), `impact_preflight.py` (call `record_hit` for surfaced entries), tests in `skills/writing-plans/tests/test_ledger_store.py`.

- [ ] Failing tests → implement → green → commit.

---

## CHUNK 2 — backend correctness

- **BE-C4** atomic compound ops: add `db.transaction(conn)` context (single BEGIN/COMMIT); wrap `evaluate_and_update_plan` sub-step inserts + revision bump, and `_open_agent_review`'s 4 writes. Test: simulate failure mid-sequence → no partial mutation. Files: `db.py`, `api.py:141-154,349-365`, `tests/test_api.py`.
- **BE-C5** DB-level COMPLETED immutability: add `BEFORE UPDATE ON Steps WHEN OLD.status='COMPLETED'` trigger (allow only summary/agent_output? — see C4 in skills audit) OR centralize; and `completed_at = COALESCE(...)`. Ship trigger in a new `011_*.sql`. Test: `db.update_step_status` on a COMPLETED step raises; re-complete doesn't re-stamp. Files: new migration, `db.py`, `tests/`.
- **BE-S1** token-boundary project match: `detect_registered_project` splits haystack into normalized tokens, tests set-membership. Files: `api.py:324-327`, `tests/test_detect_registered_project.py` (add "app" must-not-match-"happens" case).
- **BE-C6** `truncate_for_log` guard `if len(lines)>10` + `max(0,...)`. Files: `telemetry.py:23-31`, `tests/test_telemetry.py`.
- **BE-C7** fix `fail_step` docstring (or add PENDING→FAILED). Files: `api.py:188-189` (+ `state_machine.py` if behavior change; default: docs-only).

---

## CHUNK 3 — project-state-graph correctness + data

- **PSG-D2 + PSG-C1** add `run_id` to node/edge (new analyzer schema migration in `store.init_db`), scope reads to latest run; re-run no longer duplicates. Test: run analyzer twice → stable counts. Files: `analyzer/store.py`, `analyzer/cli.py`, `analyzer/*` readers, tests.
- **PSG-D1 + PSG-D3** promote `dtype`/`dtype_provenance`/`confidence` to indexed columns; add `idx_node_qualified_name`/`idx_node_name`. Files: `store.py`.
- **PSG-C2/C3/C5** qualified-name resolution in the 6 analyzers + reviewer (`contract_diff._card_for/_file_of`): resolve by module-qualified name, emit per-candidate edges with confidence; reviewer matches qualified_name first, drop silent LIMIT 1. New shared `analyzer/_index.py`. Tests: two same-named fns in different modules land edges on the correct node.
- **PSG-C4** real dtype gate: compare consumer param annotation vs producer return type. Files: `selfcheck.py`, `dataflow_types.py`, tests.
- **PSG-C6** scope `sql_contract` readers to the changed file's table(s). **PSG-C7** fix deleted-file/indented-method diffing. Files: `contract_diff.py`, `review_diff.py`, tests.

(Chunk 3 is the largest; will be elaborated to full TDD step detail when reached, after Chunks 1–2 land. Each finding above already names file + approach + test.)

---

## CHUNK 4 — skills security

- **SK-S1** base64-encode `STEP_ID`/`STEP_TYPE` like CMD/SUMMARY in `run-step.sh:77-78,87` (or stop using `eval`). Files: `run-step.sh`, `tests/test_run_step_smoke.py`.
  - **SK-T1** test: `step_id` with a space and with `$(...)` → no code execution, clean failure.
- **SK-C2** capture finalize-call rc explicitly and still `exit $EXIT_CODE`. Files: `run-step.sh:121-168`.

---

## CHUNK 5 — dashboard UX (the priority surface)

- **DASH-UX1** auto-`open` IN_PROGRESS step: `{% if s.status=='IN_PROGRESS' %}open{% endif %}` at `_dashboard_partial.html:127`.
- **DASH-UX3** failures lead: red bar when any FAILED, "N steps failed" chip, auto-open FAILED cards. `main.py:86` (progress/failed counts), `_dashboard_partial.html:33-38,129`.
- **DASH-UX4** surface review/revision "why": render per-step `summary` + FAILED terminal log as a review trail; show revision reasons from the new `Deviations` table (depends on Chunk 1). `queries.py`, `_dashboard_partial.html:30`.
- **DASH-UX2** sticky "current activity" banner (IN_PROGRESS desc + last 3 log lines).
- **DASH-UX5** relative time helper + absolute in tooltip.
- **DASH-BUG1** fix connection leak `main.py:64`. **DASH-BUG2** catch `sqlite3.Error` in poll + `/api/health`.
- **DASH-TEST1** add `TestClient` route tests + a read-only-enforcement test. New `orchestrator-webapp/tests/test_routes.py`.

---

## Per-chunk PR strategy

Each chunk is committed incrementally on `feat/plugin-packaging-and-audit` (or a sibling `feat/p2-*` branch). P2 opens as its **own PR** separate from P1. Full suite (per-suite) green before each chunk's final commit.

## Self-Review

Spec coverage: every selected finding (A/B/C bundles) maps to a chunk task above; D+E bundles intentionally excluded per maintainer selection. Migration-safety honored (010/011 additive, runner applies once). Type consistency: `VALID_STEP_TYPES` enum mirrored in the 010 trigger; `Deviations` column names consistent between 010 SQL, `insert_deviation`, and the dashboard reader (Chunk 5). Chunk 3 carries task-level (not yet step-level) detail by design — it depends on Chunks 1–2 and will be expanded on arrival.
