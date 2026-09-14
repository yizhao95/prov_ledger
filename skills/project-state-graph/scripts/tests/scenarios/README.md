# Timeline scenarios

A scenario is an ordered list of plans applied to a **synthetic project** in a
fully isolated workspace. It answers, repeatably: *when a later task changes
this node, which flows fire, and what do success and failure look like?* Every
scenario asserts an event stream (`expect.events`) **and** the things that must
not happen (`must_not`) — over-propagation and under-propagation fail alike.

## What the runner does (public write path only)

```
make_workspace   copy fixture -> git init/commit -> init_project.sh   (run 1, baseline)
for each task:
  apply_change   {files} | {generated} | {revert_to}  -> one commit
  publish        publish-plan.sh from inside the repo  (cwd attribution; no project name in the goal)
  run_steps      run-step.sh per step (COMMAND "true")
  close          review_run.py --json  (reasons from the task; --tests when given)
collect          PSG analysis_run / node_event / node_snapshot + orchestrator Plans /
                 node_reason / expectations / outcomes / review-log tags
normalize        see below
check_expect     expect.events (subset match) + must_not (any hit fails)
```

The test code never writes the orchestrator DB or the state graph directly.

Isolation: one temporary directory per scenario; `ORCH_DB`, `PSG_REGISTRY_PATH`,
`PSG_REGISTRY_ROOT` and `HOME` all point into it; `PROVLEDGER_VENV` points at
the real venv so the shell scripts find their interpreter. `~/skill-workspace`
is never touched.

LLM stages are deterministic stubs: reasons come from the scenario file,
identity arbitration is not enabled. Tests marked `llm_consistency` are
deselected by default (`pyproject.toml`).

## scenario.toml

```toml
[scenario]
name = "attribute_change"
fixture = "pipeline_repo"                            # tests/scenarios/fixtures/<name>

[[setup]]                                            # optional, runs after the baseline graph
ledger_constraint = { subject = "pkg.pipeline.clean", statement = "...", rationale = "...", visibility = "restricted" }

[[tasks]]
name = "t1"                                          # becomes the plan's prefix and its golden name
goal = "loosen the qty filter"                       # no project name — cwd attribution
declared_targets = ["pkg.pipeline.clean"]
change = { files = { "pkg/pipeline.py" = "..." } }   # or { generated = "rename_variable" } / { revert_to = "t0" }
expectations = [ { target = "pkg.pipeline.clean", target_kind = "node", claim = "fewer rows dropped", channel = "graph" } ]
reasons = "stub"                                     # "stub" | "unstated" | { "pkg.pipeline.clean" = "loosened for Q3" }
tests = "python3 -m pytest -q tests"                 # optional: review_run.py --tests
project = "none"                                     # optional: opt out of attribution (project_none_skipped only)

[expect]
events = [
  { plan = "t1", type = "node_changed", node = "pkg.pipeline.clean", changed = ["struct_sig"] },
  { plan = "t1", type = "reason", node = "pkg.pipeline.clean", text_is = "stated" },
  { plan = "t1", closed = "COMPLETED", review_state = "reviewed" },        # no type: matches any event
]
must_not = [
  { plan = "t1", type = "node_changed", node = "pkg.pipeline.load" },
  { plan = "t1", type = "identity_ambiguous" },
  { anywhere = "the restricted rationale" },                                # substring of the whole normalised JSON
]
```

`change.generated` names a mutator from `tests/corpus/mutate.GENERATED`
(`rename_variable`, `rename_function`, `strip_comments`, `add_comments`,
`reformat`). `change.revert_to` checks out every file as it was at that task's
commit and commits the result (a new commit, so the graph sees a change).

### Matching

An expectation entry matches an event when every key it names matches:
scalars by equality, lists as subsets, and the operators `<key>_contains`
(substring) and `<key>_startswith`. `expect.events` entries must each match at
least one event; a `must_not` entry must match none. `must_not` is mandatory.

### Normalised events

| type | keys |
|---|---|
| `node_added` / `node_changed` / `node_removed` / `node_matched` / `identity_ambiguous` … | `plan`, `run`, `node`, `tier`, plus the event payload (`changed`, `struct_sig`, `via`, `prev_run`, …) |
| `reason` / `constraint_ref` / `rejected_path` | `plan`, `run`, `node`, `text_is` (`stated` \| `unstated`), `text`, `source`, `tier` |
| `expectation` | `plan`, `target`, `target_kind`, `claim`, `channel` |
| `outcome` | `plan` (the expectation's), `target`, `kind`, `signal` (survival), `value`, `source`, `tier`, `reason`, `backfilled_by` |
| `plan` | `plan`, `closed`, `review_state`, `project`, `project_source`, `review_skipped` |
| `review_log` | `plan`, `step` (`REVIEW` \| `REVIEW.1`), `tag` (e.g. `[REVIEW LOCK]`, `[CONSTRAINT BYPASSED]`) |

### Normalisation rules (golden stability)

- dropped: `id`, `created_at`, `observed_at`, `updated_at`, `started_at`, `completed_at`, `commit_sha`
- `run_id` -> the run's ordinal inside the scenario (baseline = 1)
- `node_key` -> the key's latest `qualified_name`
- `plan_id` -> the task name; `step_id` -> `REVIEW` / `REVIEW.1`
- event ids inside outcome evidence -> `"<run>.<seq>"`

## Adding a scenario

1. `mkdir cases/<name>`; write `scenario.toml` (must have `must_not`).
2. `PROVLEDGER_UPDATE_GOLDEN=1 python3 -m pytest tests/test_scenarios.py -k <name>` writes `golden.json`.
3. Run it again without the variable: it must pass and the golden must be byte-identical.
4. Commit `scenario.toml` + `golden.json`. One scenario should stay under 15 s.

## Updating goldens

Only when an intended behaviour change alters the event stream:
`PROVLEDGER_UPDATE_GOLDEN=1 python3 -m pytest tests/test_scenarios.py`, then
review the diff of every `golden.json` as you would review code.
