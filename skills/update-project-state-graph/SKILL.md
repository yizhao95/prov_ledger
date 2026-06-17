---
name: update-project-state-graph
description: Use when a plan that touched a registered project reaches the NEEDS_REVIEW state, when reviewing plan completion for a project-state change, or when checking whether a git diff broke consistency with a project's deep state graph. Keywords - NEEDS_REVIEW, project state review, maintain project state, stale reference check, renamed function still called, code graph consistency, agent review close, review plan completion.
---

# Update Project State Graph

## Overview

The review sub-agent for plans that changed a **registered** project. When the
orchestrator parks a plan in `NEEDS_REVIEW` (because the plan mentions a project
in `projects.json`), the main agent dispatches you with this skill. You decide
whether the project's code change broke consistency with its deep state graph,
then finalize the plan **only** through the deterministic close script.

**Core principle:** Code gaps → FAIL the review (report, don't auto-fix). Clean →
refresh the graph + re-run tests, then close COMPLETED.

## When to Use

- A `complete-step`/`fail-step` tail line returned `needs_agent_review: true`
  with a `project` name and `review_step_id`.
- You are explicitly asked to review a plan's completion for a project-state change.

## When NOT to Use

- **Initial graph creation / onboarding a repo** → use `project-state-graph`
  (this skill consumes the graph it produces; it never builds the first one).
- A plan that mentions **no** registered project — the orchestrator already
  closes those deterministically; there is nothing for you to do.

## Inputs (from the dispatch)

- `plan_id`, `project` (canonical registry name), `review_step_id`, and
  `review_child_step_id` (the tracked child step `<plan>-REVIEW.1` you must drive).
- Registry: `~/skill-workspace/project-graphs/projects.json` →
  the project's `repo`, `db_path` (deep graph), and `commit_sha`.

## The Flow

```
1. Resolve the diff range          review_diff.resolve_range(repo, registered_sha)
      remote (origin/upstream) if an upstream exists, else local sha..HEAD
2. Parse changed symbols           review_diff.changed_symbols(repo, base, head)
      removed / renamed top-level def/class
3. Check the deep graph            review_diff.report(db_path, changed)
      stale_references: callers still pointing at removed/renamed symbols
            │
   ┌────────┴─────────────┐
   │ report.ok == False   │ report.ok == True
   ▼                      ▼
4a. FAIL the review        4b. Refresh + re-test, then close
    fail-step.sh the          - rebuild graph via project-state-graph
    child <plan>-REVIEW.1        init_project.sh (deterministic)
    (reason = the gaps;       - re-run the project's test suite
     plan auto-FAILS)         - run selfcheck on the refreshed graph
                              - complete-step.sh the child <plan>-REVIEW.1
                                 (plan auto-COMPLETES)
```

## Two close-time graph gates (deterministic, run before finalizing)

The refresh in 4b calls `init_project.sh`, whose step [4/4] runs `selfcheck.py`
on the rebuilt graph. Two of those invariants govern the review outcome:

| Check | Severity | On failure |
|---|---|---|
| `no_undefined_symbols` | **error (HARD)** | A bare-name call resolving to nothing (rename/typo). **FAIL the review** — `fail-step.sh` the child `<plan>-REVIEW.1` with the offending `name() @ file:line` list. A human decides. |
| `no_isolated_nodes` | **warning (yellow)** | Dead-code callables with no behavioral edges. **Non-blocking** — surface the `[WARN]` line(s) in the review summary, but still `complete-step.sh` the child if everything else is clean. |

Because `init_project.sh` runs with `set -e`, a `no_undefined_symbols` failure
makes the refresh itself exit non-zero — treat that as an automatic review FAIL.
The existing `stale_references` gate (below) is unchanged and runs in addition to
these.

## Data drift gates (v2 — report, don't auto-fix)

In addition to code-symbol stale references, the review now checks **data-level**
consistency via `review_diff.data_drift(db_path, removed_columns=..., dtype_changes=..., removed_datasets=...)`:

| Data gate | On failure |
|---|---|
| **removed/renamed column** still present (wired) in the graph | FAIL the review — a downstream consumer may still read it. |
| **changed dtype** that disagrees with the graph's recorded dtype | FAIL the review — walk the typed chain; the change may not "go through" downstream. |
| **removed dataset** still referenced in the graph | FAIL the review. |

Same philosophy as `stale_references`: **report and FAIL, never auto-fix** — a
human decides. `data_drift` mutates nothing in the graph. Feed it the
column/dtype/dataset deltas you derive from the diff (e.g. via the data-model
analyzer on base vs head), then fold its `ok` into the review verdict alongside
the stale-reference and selfcheck gates.

## Contract-drift gates (Phase A — base-vs-head AST fingerprints)

The `contract_diff` module adds a **shared base-vs-head fingerprint engine** that
parses the FULL AST of each side of the diff (never regex on diff text) and
compares structured contracts. Three contract types, all wired into the verdict:

| Gate (fn) | Fingerprint | FAIL when |
|---|---|---|
| **A-1 signature** `signature_contract(db, repo, base, head)` | Python param list + annotations + return annotation (body ignored) | A changed signature/return-contract whose caller (or, for return changes, `output_consumer`) lives in a file **not** in this diff. In-diff dependents are downgraded to warnings. |
| **A-2 dataframe schema** `dataframe_schema_contract(db, fn, base_cols, head_cols, changed_files)` | A producing function's `{column: dtype}` set | A dropped / renamed / retyped column whose `output_consumer` file is **not** in the diff. |
| **A-3 sql projection** `sql_contract(db, repo, base, head)` | A `.sql` query's projected output column set | A removed projection column whose downstream `reads_sql` reader file is **not** in the diff. Added columns -> warning. |

**A-4 stored assumed-schema**: the builder (`analyzer/sql_refs.py`,
`analyzer/api_refs.py`) now records the columns/keys the code *expects* a source
to return as `metadata_json["assumed_schema"]` on `sql_table` / `bq_dataset` /
`api_source` nodes. Purely additive (generic graph, no migration).

**A-5 combined verdict**: `review_diff.full_verdict(db, repo, base, head, changed=..., removed_columns=..., dtype_changes=..., removed_datasets=..., dataframe_deltas=...)`
runs **every** gate and returns `{ok, gaps, text, gates:{name: bool}}`. The
verdict is the **AND** of all gates (`ok` is False iff any gate fails). Same
philosophy as the rest of the reviewer: **report and FAIL, never auto-fix**.

## Quick Reference

| Need | Call |
|---|---|
| Pick diff range (auto remote/local) | `review_diff.resolve_range(repo, sha)` |
| Find renamed/removed symbols | `review_diff.changed_symbols(repo, base, head)` |
| Find stale callers in the graph | `review_diff.stale_references(db_path, names)` |
| Full verdict | `review_diff.report(db_path, changed)` |
| Combined contract verdict (AND of all gates) | `review_diff.full_verdict(db_path, repo, base, head, changed=...)` |
| Run graph gates (undefined=HARD, isolated=warn) | `selfcheck.run(db_path)` (also run by `init_project.sh` [4/4]) |
| Refresh the deep graph | `project-state-graph/scripts/init_project.sh` |
| Close the plan (drive the child step) | `executing-plans/scripts/complete-step.sh` / `fail-step.sh` on `<plan>-REVIEW.1` |

Run the helpers with the project-state-graph venv:
`~/skill-workspace/orchestrator/.venv/bin/python` (stdlib-only module).

## Close Contract (non-negotiable)

- You are assigned the tracked child step `review_child_step_id`
  (`<plan>-REVIEW.1`). `start-step` it, do the review, then finalize **that
  child step**:
  - gaps found → `fail-step.sh` the child (plan auto-closes FAILED),
  - clean → refresh graph + re-run tests, then `complete-step.sh` the child
    (plan auto-closes COMPLETED).
- The deterministic procedure finalizes the review step + plan **from the child
  outcome** — do NOT touch the `<plan>-REVIEW` step or plan status directly, and
  never edit the DB.
- **Fallback only:** `agent-review-close.sh {plan_id, outcome, summary,
  log_context}` still works for a plan with no child step (legacy/in-flight) and
  keeps any child in sync. Prefer driving the child step.

## Common Mistakes

| Mistake | Fix |
|---|---|
| Auto-fixing the stale caller | Don't. Report it and FAIL the review — a human decides. |
| Treating an isolated-node warning as a blocker | It's yellow/non-blocking — surface it, don't FAIL on it. |
| Closing COMPLETED despite a `no_undefined_symbols` failure | That's a HARD gate — FAIL the review; never close over undefined symbols. |
| Diffing the wrong range | Use `resolve_range`; never hand-pick base/head. |
| Closing COMPLETED without refreshing the graph | A clean review MUST refresh the graph + re-run tests first. |
| Building the graph from scratch here | Wrong skill — that's `project-state-graph`. |
| Skipping the close | The plan stays stuck in NEEDS_REVIEW. Always finalize the child step (`complete-step`/`fail-step` on `<plan>-REVIEW.1`). |

## When the project has no deep graph yet

If `db_path` is missing, the project was never onboarded — FAIL the review with a
note to run `project-state-graph` first. Do not silently skip the graph check.
