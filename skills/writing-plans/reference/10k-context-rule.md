# 10k Context Threshold Rule

While decomposing a plan, **estimate the context size of each step**. If a step is estimated to consume more than ~10,000 tokens of context, you MUST tag it `SUB_AGENT` and plan for it to be dispatched via the `subagent-driven-development` skill — NOT executed inline in the main session.

## What counts toward the 10k estimate

- Reading large files (>500 lines)
- Multi-file refactors touching 3+ files
- Large repo scans / greps that surface long results
- Long debugging traces or stack inspections
- Heavy reference docs (API specs, large schemas, vendor docs)
- Any step whose intermediate scratch work would balloon

## Decision matrix at planning time

| Estimated step context | step_type | Notes |
|---|---|---|
| ≤ 10k tokens | `THINKING` / `ANALYSIS` / `CODE` / `COMMAND` / `DOCUMENTATION` | Execute inline in main session |
| > 10k tokens (single heavy step) | `SUB_AGENT` | Dispatch ONE sub-agent (sequential mode in `subagent-driven-development`) |
| > 10k tokens AND ≥ 2 such steps are mutually independent (no shared state, no sequential dependency) | `SUB_AGENT` × N | Plan to dispatch them in PARALLEL (Part B of `subagent-driven-development`); record each as its own row, mark them as parallel-safe in the description (e.g., `SUB_AGENT [parallel-safe]: ...`) |

## Recording the estimate

For any step you tag `SUB_AGENT`, attach `ctx_estimate_k` to the step object in plan-input — and add a `[parallel-safe]` tag to the description when applicable:

```json
{
  "description": "SUB_AGENT [parallel-safe]: refactor user-auth subsystem",
  "type": "SUB_AGENT",
  "ctx_estimate_k": 15
}
```

## Why

Spending 10k+ tokens of main-session context on one step crowds out the orchestration view, degrades quality on subsequent steps, and makes recovery harder. A sub-agent runs in isolation, returns a curated summary, and the main session stays sharp. Multiple parallel sub-agents recover wall-clock time when heavy work is independent.

## Anti-patterns at planning time

| ❌ Wrong | ✅ Right |
|---|---|
| Tag a 20k-token refactor as `CODE` and plan to do it inline | Tag as `SUB_AGENT`, set `ctx_estimate_k`, plan dispatch |
| Plan 4 independent heavy refactors as 4 sequential `SUB_AGENT` steps | Plan them as parallel-safe so executing-plans can dispatch concurrently |
| Skip the estimate ("I'll figure it out at execution time") | Estimate during planning — executing-plans CAN deviate later, but the plan should be honest from the start |
| Tag a 1k-token step as `SUB_AGENT` | Don't over-dispatch — sub-agents have overhead; only use them when warranted |

## Downstream

The `executing-plans` skill enforces this rule at runtime — if it discovers a non-`SUB_AGENT` step actually needs >10k context once execution begins, it deviates the plan via `$CLI evaluate --deviation` and re-tags the step `SUB_AGENT`.
