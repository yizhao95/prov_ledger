# provLedger — Audit Findings (P0)

**Date:** 2026-06-25 · read-only audit, 4 parallel agents across the 4 subsystems.
Grading: **Risk** (chance × blast radius) × **Value** (payoff of fixing). **Phase:**
`P1` = fix during packaging (needed for a green, reproducible baseline on a clean
machine); `P2` = optimization, maintainer-selected. IDs namespaced by area:
`BE` backend, `PSG` project-state-graph, `DASH` dashboard, `SK` skills/CLI.

---

## 0 · Confirmed baseline reality (reproduced this session)

The documented baseline ("464 passed, 1 skipped") is **not reproducible on a clean
machine.** Actual on a fresh unified venv:

| Suite | Result |
|---|---|
| orchestrator-backend | 111 passed |
| orchestrator-webapp | 11 passed |
| **skills/writing-plans** | **3 failed**, 39 passed, 2 skipped |
| skills/executing-plans | 50 passed |
| skills/project-state-graph | 198 passed, 1 skipped |
| skills/update-project-state-graph | 50 passed |

**Root cause [SK-BASE]** — `skills/writing-plans/tests/test_ledger_cli.py:18-22`
hardcodes `ORCH_ROOT = ~/skill-workspace/orchestrator` and
`PYBIN = ORCH_ROOT/.venv/bin/python`, bypassing the correct `conftest.py`
resolution (which prefers the bundled `orchestrator-backend/`). On any machine
without that hand-made venv, the 3 `ledger_cli` tests die with
`FileNotFoundError`. **Risk: High · Value: High · Phase: P1.** Fix: use
`sys.executable` (ledger_cli.py is stdlib-only) and rely on `conftest.py` for the
import path. This is the gating fix that makes "green suite anywhere" true.

---

## 1 · orchestrator-backend

**Correctness**
- **BE-C1** Deviation justification is never persisted (return key `justification_logged` is misleading) — `orchestrator/api.py:154-160` — Risk High · Value High · **P2** (data-model). Audit trail of *why* a plan deviated is dropped.
- **BE-C2** Migration 007 silently drops the `step_type` CHECK constraint — `migrations/007_needs_review_status.sql:30` — Risk Med · Value High · **P2**. DB-level enforcement of valid step types gone after 007.
- **BE-C3** Migration 007 loses `idx_steps_type` — `007:75-77` vs `002:18` — Risk Med · Value Med · **P2**.
- **BE-C4** Compound ops span many independent commits (non-atomic) — `api.py:141-154, 349-365` — Risk Med · Value High · **P2**. Crash mid-sequence leaves plans half-mutated.
- **BE-C5** COMPLETED-step immutability enforced only at API layer; `db.update_step_status` bypasses it and re-stamps `completed_at` — `db.py:192-209` — Risk Med · Value Med · **P2**.
- **BE-C6** `truncate_for_log` yields negative "lines elided" for few-but-long lines — `telemetry.py:23-31` — Risk Low · Value Low · **P2**.
- **BE-C7** `fail_step` docstring contradicts the state machine (PENDING→FAILED rejected) — `api.py:188-189` — Risk Low · Value Low · **P2**.

**Data model**
- **BE-D1** No history/audit table for revisions & deviations — Risk Med · Value High · **P2**. New `Deviations(...)` table (migration 010).
- **BE-D2** Missing `updated_at` on Plans/Steps — Risk Low · Value High · **P2**. Enables staleness + incremental dashboard refresh.
- **BE-D3** Failure reason buried in truncatable `log_context` — `api.py:194-196` — Risk Med · Value High · **P2**. Promote to `Steps.failure_reason` (+ `attempt_count`).
- **BE-D4** A plan parked for agent review is indistinguishable from normal IN_PROGRESS — Risk Med · Value Med · **P2**. Add `Plans.review_state`.
- **BE-D5** `LedgerEntries.subjects/keywords` opaque JSON, unindexable — `009:16-17` — Risk Low · Value Med · **P2**. Normalize or FTS5.
- **BE-D6** Step-type / skill-source enums duplicated Python↔SQL (drift) — Risk Low · Value Med · **P2**.

**Security / Perf / Boundaries / Tests**
- **BE-S1** `detect_registered_project` substring match over-eager (e.g. "app" matches "happens") — `api.py:324-327` — Risk Med · Value Med · **P2**. Token-boundary match.
- **BE-P1** N+1 in recovery walk / failed-step classification — `api.py:228-246, 516-543` — Risk Low · Value Med · **P2**.
- **BE-B1** `review_and_complete` ~230-line, 4 concerns; api.py is the largest module — Risk Low · Value Med · **P2**. Extract `review.py`.
- **BE-B2** `STARTING` is a dead state — `state_machine.py:20,33` — Risk Low · Value Low · **P2**.
- **BE-T2** No test for migration 007's rebuild (highest-risk migration, untested) — Risk Med · Value High · **P2** (pairs with BE-C2/C3).
- **BE-T4** No concurrency test; `open_db` sets no `busy_timeout`/WAL — `db.py:16-23` — Risk Med · Value Med · **P2**.

## 2 · project-state-graph (Pillar B)

**Correctness**
- **PSG-C1** Re-running a build on an existing DB **silently duplicates the entire graph** (no upsert/run scoping; self-check can't catch it) — `analyzer/store.py:64`, `cli.py:69-86` — Risk High · Value High · **P2**.
- **PSG-C2** Simple-name callable resolution (`{name: nid}`, last-wins) binds edges to the **wrong same-named symbol** across 6 analyzers — Risk High · Value High · **P2**. Corrupts the impact analysis the system exists for.
- **PSG-C3** `dataflow_types` resolves ambiguous names to `ids[0]` → 2nd+ same-named fns get no produces/feeds — `dataflow_types.py:154-160` — Risk Med · Value High · **P2**.
- **PSG-C4** `dtype_consistency_e2e` is a tautology (compares a value to itself) — the ERROR gate can never fire on real graphs — `selfcheck.py:195-227` — Risk Med · Value High · **P2**.
- **PSG-C5** Contract reviewer resolves dependents/cards by bare name `LIMIT 1` → checks against the wrong function (false PASS) — `contract_diff.py:147-165` — Risk High · Value High · **P2**.
- **PSG-C6** `sql_contract` flags every `reads_sql` reader for any changed `.sql` (no table mapping) → gate noise — `contract_diff.py:398-422` — Risk Med · Value High · **P2**.
- **PSG-C7** `changed_symbols` mishandles deleted files & indented methods — `review_diff.py:52,66-79` — Risk Med · Value Med · **P2**.

**Data model / Perf / Security / Dup / Tests**
- **PSG-D1** dtype/data_class/confidence live only in `metadata_json` (unindexable) — `store.py:26-35` — Risk Med · Value High · **P2**. Promote to indexed columns (open redesign target).
- **PSG-D2** No `run_id` on node/edge — no run isolation/history/in-DB diff — `store.py:26-47` — Risk Med · Value High · **P2**. Fixes PSG-C1 cleanly.
- **PSG-D3** Missing index on `node.qualified_name`/`name` (most-queried) — Risk Low · Value High · **P2**.
- **PSG-D4** `column` nodes have no edge to the producing function (column lineage empty) — Risk Low · Value Med · **P2**.
- **PSG-P1** `commit()` after **every** add_node/add_edge — tens of thousands of fsyncs — `store.py:80,114,135` — Risk Med · Value High · **P2**. Single transaction → order-of-magnitude speedup.
- **PSG-P2** Every `.py` opened & `ast.parse`d ~13× (dataflow_types 3× alone) — Risk Med · Value High · **P2**. Shared AST cache.
- **PSG-S1** Untrusted repo content injected raw into generated viz HTML (`</script>` breakout / stored XSS) — `graph_viz.py:393`, `slice_viz.py:415,499` — Risk Med · Value Med · **P2**.
- **PSG-X1** `graph_viz.py` byte-for-byte duplicated across both skills (397 lines ×2) — Risk Med · Value Med · **P2**.
- **PSG-X2** `slice_viz.py` (503) & `graph_viz.py` (397) oversized (py+HTML+JS+SQL in one) — Risk Low · Value Med · **P2**.
- **PSG-T1** No test re-runs analyzer on an existing DB (PSG-C1 uncovered) — Risk High · Value High · **P2**.
- **PSG-T2** No test that same-name edges land on the correct target — Risk High · Value High · **P2**.

## 3 · Dashboard (the priority review surface)

**UX (lead with these)**
- **DASH-UX1** IN_PROGRESS step doesn't auto-expand → the live log (the app's whole point) is hidden until you click — `_dashboard_partial.html:127` — Risk Med · Value High · **P2** (small). Add `{% if s.status=='IN_PROGRESS' %}open{% endif %}`.
- **DASH-UX3** Failure is invisible above the fold (bar always green, no failed rollup, failed cards collapsed) — `_dashboard_partial.html:33-38` — Risk Med · Value High · **P2**.
- **DASH-UX4** Review/revision activity reduced to a bare counter — no "why" — `_dashboard_partial.html:30` — Risk Med · Value High · **P2**. Biggest content gap for the review use case.
- **DASH-UX2** No sticky "current activity" banner; logs siloed per-step — Risk Low · Value High · **P2**.
- **DASH-UX5** Raw machine timestamps, no relative time — Risk Low · Value Med · **P2**.
- **DASH-UX6** Non-adjacent parallel siblings lose the side-by-side grid — `_dashboard_partial.html:217-241` — Risk Low · Value Med · **P2**.
- **DASH-UX7** `/history` has no pagination/search/filter — Risk Low · Value Med · **P2**.
- **DASH-UX9** Log truncated to last 10 with no way to see the rest (failed step's error may be hidden) — Risk Med · Value Med · **P2**.

**Correctness / Perf / Tests**
- **DASH-BUG1** Connection leak on plan-not-found path — `main.py:64` — Risk Med · Value High · **P2** (cheap).
- **DASH-BUG2** Only `FileNotFoundError` handled; DB lock/corruption 500s polls and `/api/health` — `main.py:142-183` — Risk Med · Value Med · **P2**.
- **DASH-PERF2** `compute_etag` full-scans 3 tables every 2s per client; `/history` unbounded — Risk Med · Value Med · **P2**.
- **DASH-TEST1** No route-layer/DB-query tests; no read-only-enforcement test — Risk Med · Value High · **P2**.

## 4 · skills / CLI / harness

**Security / Correctness**
- **SK-S1** run-step `eval`s raw `step_id`/`type` → command-injection sink (`x$(touch …)`) — `run-step.sh:77-78,87` — Risk High · Value High · **P2**. base64-encode them like cmd/summary.
- **SK-C1** `PYBIN` can be unbound under `set -u`; friendly guard is dead code; 8 executing-plans wrappers omit the guard entirely — `publish-plan.sh:31-37` et al. — Risk Low · Value Med · **P2** (fold into SK-DC1).
- **SK-C2** `set -e` after the wrapped command can swallow the true exit code — `run-step.sh:121-168` — Risk Med · Value Med · **P2**.
- **SK-C3** Truncation counts chars not bytes despite "BYTES" marker — `_truncate_log.py:20-38` — Risk Low · Value Low · **P2**.

**Harness quality**
- **SK-H1** A `TEST` step type is referenced everywhere but doesn't exist (enum has 6 types); TDD pairing rule unsatisfiable as written — Risk Med · Value High · **P2**.
- **SK-H2** `tdd-test-spec.md:64` claims an auto-enforcement that isn't implemented **and** tells you to call `orchestrator-cli.py evaluate` directly, contradicting SKILL.md's "never call the CLI" — Risk Med · Value High · **P2**.
- **SK-H3** "5-step workflow" actually lists 6 — `writing-plans/SKILL.md:18-46` — Risk Low · Value Med · **P2**.
- **SK-H4** Script count stated as 7/8/9 in different places — `executing-plans/SKILL.md` — Risk Low · Value Med · **P2**.
- **SK-H5** Hardcoded `~/.code_puppy/…` and `~/skill-workspace/orchestrator/.venv` paths in SKILL examples conflict with the self-contained clone (dead copy-paste) — Risk Med · Value Med · **P2** (overlaps SK-BASE; relevant to packaging).
- **SK-H6** `subagent-driven-development/SKILL.md` oversized (486 lines), hardcodes volatile model ids — Risk Low · Value Med · **P2**.

**Data / Dup / Tests**
- **SK-D1** LedgerEntries lacks `superseded_by`/`updated_at`/`plan_id`/`hit_count` provenance+ranking columns — Risk Low · Value High · **P2** (migration 010, backward-compatible).
- **SK-D2** Fuzzy match is unweighted set-overlap, ignores statement/rationale text — `impact_preflight.py:318-341` — Risk Low · Value Med · **P2**.
- **SK-DC1** Identical ~6-line PYBIN block copied into **11** shell scripts (the biggest shell maintenance liability) — Risk Low · Value High · **P2**. Extract `scripts/_pybin.sh`. (This is also where the unified-venv wiring of P1 Task 4 should ideally live.)
- **SK-T1** No test for malicious/whitespace `step_id` (guards SK-S1) — Risk Med · Value Med · **P2**.

---

## Cross-cutting top 10 (highest risk × value)

1. **SK-BASE** — fix the hardcoded test interpreter so the suite is green on any machine. *(P1, gating)*
2. **PSG-C1 + PSG-D2** — re-running a build duplicates the whole graph; add `run_id` + scoped reads.
3. **PSG-C2/C3/C5** — same-name symbol resolution binds edges (and the merge gate) to the wrong function; qualify resolution.
4. **SK-S1** — run-step `eval` command-injection; base64-encode `step_id`/`type` (+ SK-T1).
5. **BE-C1 + BE-D1/D3** — deviation justification & failure reasons are dropped/truncatable; promote to first-class tables/columns (migration 010).
6. **PSG-C4** — the ERROR-severity dtype gate is a tautology that can never fire; compare consumer-expected vs producer-actual.
7. **DASH-UX1/UX3/UX4** — make the live log auto-visible, make failures lead, surface review "why" — the core review-surface wins.
8. **PSG-P1 + PSG-P2** — per-insert commits + ~13× re-parsing; single transaction + shared AST cache (big build speedup).
9. **BE-C2/C3 (+BE-T2)** — migration 007 dropped the step_type CHECK and an index; restore via migration 010 with a regression test.
10. **SK-H1/H2 + SK-DC1** — fix the harness's most damaging doc inconsistencies (phantom TEST type, false TDD enforcement) and de-duplicate the 11-copy PYBIN block.

## Suggested P2 bundles (for selection)

- **Bundle A — Reliability & correctness** (BE-C1/C2/C3/C4/C5, PSG-C1–C7, SK-S1/C2): the real bugs. Highest risk reduction.
- **Bundle B — Data-model & schema (migration 010)** (BE-D1/D2/D3/D4, PSG-D1/D2/D3, SK-D1): the maintainer-opened redesign; new columns for future tasks.
- **Bundle C — Dashboard UX** (DASH-UX1/2/3/4/5 + BUG1/BUG2 + TEST1): the priority review surface.
- **Bundle D — Performance** (PSG-P1/P2/P3/P4, BE-P1, DASH-PERF2): build-time & per-request speedups.
- **Bundle E — Harness & dedup hygiene** (SK-H1–H6, SK-DC1, SK-C1): runnable docs + one shared PYBIN helper.
