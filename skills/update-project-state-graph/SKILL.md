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

The `project` is the plan's own attribution (`Plans.project` /
`project_source` — declared, derived from the publishing repo, or `legacy`),
not something inferred from the goal text (FL-014, phase 3.5).

- `plan_id`, `project` (canonical registry name), `review_step_id`, and
  `review_child_step_id` (the tracked child step `<plan>-REVIEW.1` you must drive).
- Registry: `~/skill-workspace/project-graphs/projects.json` →
  the project's `repo`, `db_path` (deep graph), and `commit_sha`.

## Run it: `scripts/review_run.py` (phase 4)

The flow below is **executed by a script**, not walked by hand. The agent's job
shrinks to three decisions: which test command to re-run, whether a failing
`signature` or `stale_references` gate (or a dirty working tree) is acceptable on
the record, and what the reasons are.

```bash
PY=~/skill-workspace/.venv/bin/python
# 1. look before writing anything (steps 0–3; it PRINTS the 4b/4c gates it did NOT evaluate,
#    so a PASS here is not a PASS for the real run; exit 1 = the gates it did check would fail)
$PY skills/update-project-state-graph/scripts/review_run.py --plan-id <plan> --project <name> --dry-run
# 2. run it: lock -> gates -> refresh (PROVLEDGER_* attribution) -> tests -> selfcheck -> checklist
$PY .../review_run.py --plan-id <plan> --project <name> --tests "<suite command>" --reasons ask
#    exit 6 + the closed checklist; REVIEW.1 stays IN_PROGRESS
# 3. answer every slot in one sentence ("unstated" when you do not know), then resume at 4c
$PY .../review_run.py --plan-id <plan> --project <name> --reasons reasons.json     # -> complete-step REVIEW.1
```

| Flag | Meaning |
|---|---|
| `--reasons stub\|unstated\|ask\|<file.json>` | `stub` = "scenario: <event_types>" per slot (scenario tests only); `unstated` = every slot NULL; `ask` = print the checklist and stop (exit 6, resumable); file = `[{qualified_name\|node_key, text}]`, an unknown key exits 6 and writes nothing |
| `--tests "<cmd>"` | the 4b re-test, run inside the repo; omitted = logged as *tests skipped*, never silent |
| `--accept-signature "<reason>"` | may override the `signature` gate **only** (FL-018 additive kwargs); the reason is written into the REVIEW.1 log and summary; any other failing gate still fails |
| `--accept-stale "<reason>"` | may override the `stale_references` gate **only** — a definition that moved and is re-exported (the graph does not follow imports); logged the same way. It combines with `--accept-signature`; a failing gate that neither covers still fails |
| `--allow-dirty` | refresh even though tracked files have uncommitted changes (the refresh analyses the working tree; untracked files do not count); logged as a `[MANUAL VERDICT]`. Without it a dirty tree FAILs the review at 4b |
| `--timeout-tests <seconds>` | ceiling on the `--tests` command; default **600** s |
| `--timeout-graph <seconds>` | ceiling on the `init_project.sh` refresh; default **4800** s |
| `--dry-run` | steps 0–3, nothing written (no lock line, no start-step, no refresh). Its report **names every gate it did not evaluate** — `dirty_working_tree`, `graph_refresh`, `tests`, `selfcheck` (4b) and `close_reasons` (4c) — so a dry-run PASS can never be read as a real-run PASS. A dirty working tree passes the dry run and FAILs the real one (FL-134) |
| `--as-recovery <step>` | an attempt FAILED: run 1–4c **as** that PENDING retry. The retry is a **child of the attempt it retries** (`REVIEW.1.1`, then `REVIEW.1.1.1`) — never a sibling; the driver refuses a sibling of an unrecovered attempt (exit 6) and names the step to nest under (FL-138) |
| `--json` | machine-readable result as the last stdout line: `{verdict, gates, range, refreshed_sha, slots, filled, unstated, closed}`, plus `{dry_run, not_evaluated}` on a dry run |
| `--registry <projects.json>` | isolated registry (tests / scenarios); defaults to `PSG_REGISTRY_PATH` or `~/skill-workspace/project-graphs/projects.json` |

**Time ceilings (every subprocess has one).** The review rebuilds the whole graph
and runs the project's own suite, so it is minutes long — but minutes must have an
end: *a tool that gets slower in silence is the thing this project exists to
prevent.*

| Call site | Ceiling | Overridable |
|---|---|---|
| the executing-plans write scripts (`append-log`, `start-step`, `reason-slots`, `reason-fill`, `complete-step` / `fail-step`) | **60** s (fixed — one sqlite write each; past that it is wedged, not slow) | no |
| the graph refresh (`init_project.sh`) | **4800** s | `--timeout-graph` |
| the `--tests` command | **600** s | `--timeout-tests` |

A breach takes the **same path as a non-zero exit**: the refresh and the tests
`fail-step` `<plan>-REVIEW.1` with the ceiling and the elapsed seconds in the
reason (exit 1); a write script that hangs exits 5. A timeout never leaves the
plan quietly `IN_PROGRESS`, and it is never reported as a pass.

Exit codes: `0` **the plan closed COMPLETED** — and nothing else · `1` it did not:
closed FAILED (4a gaps, refresh / tests / selfcheck failure or **timeout**), or
left short of COMPLETED because something at close time refused (an unfinished
sibling recovery step, a registry behind HEAD) · `5` a write script failed
(including its 60 s timeout) · `6` bad input, including an `--as-recovery` step of
the wrong shape. Every write
still goes through the executing-plans scripts (`append-log`, `start-step`,
`reason-slots`, `reason-fill`, `complete-step` / `fail-step`) — the driver adds
no second write path. If `REVIEW.1` is already `IN_PROGRESS` the driver resumes
at 4c (no second refresh).

## The Flow (what the driver does)

```
0. LOCK the registered sha         registered_sha = registry["commit_sha"]  (read it FIRST,
      write it into the review step's log, and only then do anything else —
      a refresh moves the registry to HEAD and would make the range empty;
      pass it to full_verdict(..., registered_sha=registered_sha): an empty
      range while HEAD != registered_sha FAILS the review — E6-4)
1. Resolve the diff range          review_diff.resolve_range(repo, registered_sha)
      registered sha..HEAD when that sha is an ancestor of HEAD (the normal case,
      pushed or not); else merge-base(upstream, HEAD)..HEAD; else sha..HEAD (local)
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

**Before the refresh in 4b** export the run attribution so the rebuilt graph's
`analysis_run` row (and every history event it appends) points back at this
review step (spec §2.5):

```bash
export PROVLEDGER_PLAN_ID=<plan_id> PROVLEDGER_STEP_ID=<plan_id>-REVIEW.1 PROVLEDGER_TRIGGER=review
bash skills/project-state-graph/scripts/init_project.sh --name <project> --repo <repo>
```

**4c. Reasons (after the refresh, before completing the child step)** — spec §2.8:

```bash
bash skills/executing-plans/scripts/reason-slots.sh <in.json>   # {plan_id, project}
#   -> prints a CLOSED checklist: the N data points this plan's runs changed
#      (function / class / column node_keys). Answer each in one sentence;
#      write "unstated" when you do not know — never invent.
bash skills/executing-plans/scripts/reason-fill.sh <in.json>    # {plan_id, project, run_id, reasons: [{node_key, text}]}
bash skills/executing-plans/scripts/complete-step.sh <in.json>  # the child <plan>-REVIEW.1
#   -> at close the system backstops every unanswered key as unstated (NULL,
#      source=system) and turns deviation justifications into rejected_path
#      rows. Missing reasons never block the close; they stay visible.
```

`init_project.sh` no longer deletes the DB before rebuilding: `node_snapshot` /
`node_event` accumulate across refreshes and `python3 -m analyzer history <db>
<qualified_name>` (run from `skills/project-state-graph/scripts`) shows a
node's event stream with the plan/step that caused each change.

## Evidence (4d — after the reasons are filled, before completing the child step)

A reason says **what kind of statement** it is. Evidence says **how checkable**
it is. They are two dimensions and they never move each other: attaching an email
to a reason does not change its tier, it only raises its source level. Evidence
sits exactly where a test sits — a test does not change what the code does, it
changes your confidence that it does it.

**provLedger searches nothing.** It hands you a list; you search with *your* own
communication tools and *your* own credentials, and write back what you found.
No token, no mailbox and no permalink contents ever enter the ledger — only a
label, a time and a link.

### Step 1 · fill the reason from what the user said in this task's window

This is 4c above, and it happens **first**. Only what the user said in this
task's window can become a reason:

| What you have | What you write | Tier |
|---|---|---|
| The user's own words name this node | `{"node_key": "nk_…", "utterance_id": 42, "span": [0, 31]}` | `stated` |
| The user said something related but never named it, or the plan itself says what was being done | `{"node_key": "nk_…", "interpretation": "…"}` | `asserted` |
| The user said nothing about it | `{"node_key": "nk_…", "unstated": true}` | `unstated` |

Reporting the task faithfully is not a guess: "the plan says to exclude EMEA from
the rollup" is a restatement and belongs as `asserted`. Your own theory about
*why* is not in the task and not in the user's words, and there is nowhere to put
it.

### Step 2 · search your own communication tools and attach what you find

For each slot the command below gives you, search **inside its `window`** for its
`hints` — email, chat, meeting notes, tickets, whatever you have. No such tool on
this host? Skip it; nothing breaks, the source level just stays lower.

A pointer hangs on a reason, so step 1 must have left a row for it: the slot's
`reason_id` is the one to pass, and a slot still showing `"reason_id": null` has
not been filled yet — go back and fill it, `{"unstated": true}` included.

```bash
# the to-check list: node, what changed, this plan's own time window, the
# identifiers to search on, the reason id to hang a pointer on, significance
provledger review evidence-slots --plan <plan_id> --json

# one pointer per hit, hung on the reason the slot names
provledger reference add --reason <reason_id> \
  --kind email|chat|meeting|ticket|doc|commit \
  --uri <permalink> --label "<subject · who>" --occurred-at "<when>"

# a hit that disagrees with the reason — recorded, never discarded
provledger reference add --reason <reason_id> --kind email \
  --uri <permalink> --label "<subject · who>" --occurred-at "<when>" \
  --stance contradicts

# what became of each slot — write one row per slot, always
provledger review evidence-log --plan <plan_id> --node <node_key> \
  --outcome attached|found_nothing|timed_out|not_searched \
  [--reason-tier stated|asserted|derived|unstated] \
  [--evidence-level linked|verbal|task_context|unstated] \
  [--tool-hint "<which tool looked>"] [--elapsed-ms <n>]

# read it back later: "why is this one blank?"
provledger review evidence-log --plan <plan_id> --json
```

At most `PROVLEDGER_EVIDENCE_SLOTS` slots (default 5) come back, in descending
significance — that is the cost gate. The whole evidence pass has its own budget
(4800 s by default); when it runs out, log `timed_out` for the slots that were in
flight and `not_searched` for the ones that never started, and leave the reasons
as they are.

### The four rules

1. **Attaching evidence never changes a tier.** A pointer raises
   `evidence_level` (`unstated` / `task_context` → `verbal` → `linked`) and
   nothing else. Never re-fill a reason as `stated` because you found an email:
   `stated` means *the user's own words*, and a span into their `utterance` is the
   only way to get it.
2. **Attach every hit, not the best one.** Three emails and a meeting → four
   `reference add` calls. Picking the most convincing one is editing the record;
   the table is many-to-many precisely so you do not have to choose.
3. **A hit that contradicts the reason is attached with `--stance contradicts`,
   never discarded.** Evidence is allowed to fail, the same way a test is. A
   contradicting source does not demote the reason's tier — the user still said
   what they said — it tells the reader the two disagree.
4. **When both steps come up empty, write `unstated`, and never guess.** A
   pointer found in a mailbox is *evidence*, not a reason: you may not read a
   reason out of an email and fill it in. Write the gap as a row first —
   `{"node_key": "nk_…", "unstated": true}` through `reason-fill`, which is what
   gives the slot a `reason_id` — then hang the pointer on that `unstated` row and
   leave the reason itself blank. A gap deserves to be a row rather than an
   absence, and the page then says "there is a source that may be related, but
   nobody said this change was because of it", which is far more honest than a
   sentence that reads well. Only when the slot's
   `significance_level` is `major` is it worth interrupting the user once: show
   them the pointer and the change and ask "is this the reason?". If they answer,
   that answer is **their words** — record it with `provledger note` (or the hook
   records it) and fill the slot as `stated` with a span into it. If they do not
   answer, or say no, it stays blank.

**Every slot gets an `evidence_log` row, including the ones you never searched.**
That row is the answer to "why is this one blank" — nobody looked, the window
held nothing, or the look ran out of time. A system that degrades in silence is
the thing this product exists to prevent, so the silence is the one thing that
may not go unrecorded.

## Close-time graph gates (deterministic, run before finalizing)

The refresh in 4b calls `init_project.sh`, whose stage [4/5] runs `selfcheck.py`
on the rebuilt graph, and the driver runs `selfcheck.run` once more after
`--tests`. Every **error**-severity invariant FAILs the review and no warning
ever does; the list lives in `project-state-graph/scripts/selfcheck.py`. The two
that come up most often in a review:

| Check | Severity | On failure |
|---|---|---|
| `no_undefined_symbols` | **error (HARD)** | A bare-name call resolving to nothing (rename/typo). **FAIL the review** — `fail-step.sh` the child `<plan>-REVIEW.1` with the offending `name() @ file:line` list. A human decides. |
| `no_isolated_nodes` | **warning (yellow)** | Dead-code callables with no behavioral edges. **Non-blocking** — surface the `[WARN]` line(s) in the review summary, but still `complete-step.sh` the child if everything else is clean. |

Because `init_project.sh` runs with `set -e`, an error-severity failure (such as
`no_undefined_symbols`) makes the refresh itself exit non-zero — an automatic
review FAIL. These run in addition to the diff gates of step 3 (`stale_references`
and the contract gates below).

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
| Pick diff range (registered / remote / local) | `review_diff.resolve_range(repo, sha)` |
| Find renamed/removed symbols | `review_diff.changed_symbols(repo, base, head)` |
| Find stale callers in the graph | `review_diff.stale_references(db_path, names)` |
| Full verdict | `review_diff.report(db_path, changed)` |
| Combined contract verdict (AND of all gates) | `review_diff.full_verdict(db_path, repo, base, head, changed=...)` |
| Run graph gates (error severity = HARD, warnings never block) | `selfcheck.run(db_path)` (also run by `init_project.sh` [4/5]) |
| Refresh the deep graph | `project-state-graph/scripts/init_project.sh` |
| Close the plan (drive the child step) | `executing-plans/scripts/complete-step.sh` / `fail-step.sh` on `<plan>-REVIEW.1` |
| The whole flow, deterministically | `scripts/review_run.py --plan-id P --project X [--tests ...] [--reasons ...]` |
| What still needs a source, and where to look | `provledger review evidence-slots --plan <id> --json` |
| Hang one pointer on a reason | `provledger reference add --reason <id> --kind email --uri … --label … --occurred-at …` |
| Record what became of a slot | `provledger review evidence-log --plan <id> --node <key> --outcome …` |

Run the helpers with the same `PY` as `review_run.py` above; they are stdlib
only.

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
| Reading a reason out of an email you found | That is a guess, and a guess that looks sourced is worse than a blank. Attach the pointer to the `unstated` row and leave the reason empty. |
| Re-filling a reason as `stated` because evidence turned up | `stated` means the user's own words. Evidence moves `evidence_level`, never the tier. |
| Dropping a source that contradicts the reason | Attach it with `--stance contradicts`. Evidence is allowed to fail. |
| Leaving a slot out of `evidence_log` because nothing happened | Nothing happening is the row worth having — `not_searched` is why it is blank. |
| Walking steps 0–4c by hand | Use `review_run.py`; hand-driving skips the lock line, the attribution env or the checklist sooner or later (phase 2–3.5 dogfood). |
| Overriding any gate but `signature` / `stale_references` | `--accept-signature` and `--accept-stale` each cover exactly one gate, with the reason on the record. `--accept-stale` is for a moved, re-exported definition, never for a caller that really is stale; data drift, a SQL contract break or an empty range is a real gap — fix the code or FAIL. |

## When the project has no deep graph yet

If `db_path` is missing, the project was never onboarded — FAIL the review with a
note to run `project-state-graph` first. Do not silently skip the graph check.
