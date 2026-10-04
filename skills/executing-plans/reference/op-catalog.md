# Op Catalog — Full Reference

Per-op deep-dive: input shape, state transition, output JSON, common mistakes, link to test case.
`run-step`, `agent-review-close`, `reason-slots`, `reason-fill` and `headline-respond` are covered in
[`SKILL.md`](../SKILL.md); every op's input shape is in [`update-input.schema.json`](../update-input.schema.json).

All scripts share the same shape:
```bash
bash ${CLAUDE_PLUGIN_ROOT}/skills/executing-plans/scripts/<op>.sh path/to/input.json
```
Env: `ORCH_DB` overrides the SQLite path (default `~/skill-workspace/orchestrator.db`).
Output: pretty-printed JSON to stdout, then a one-line `{"ok":true,"op":…}` marker as the last line.
Errors: `❌ apply_op: <msg>` to stderr, non-zero exit.

---

## 1. `start-step.sh` — PENDING/STARTING → IN_PROGRESS

**Input** (required: `step_id`):
```json
{"step_id": "deep-health-20260514153045-A", "type": "ANALYSIS", "agent_input": "(SUB_AGENT only)"}
```

**Side effects**:
- `Steps.status` = `IN_PROGRESS`
- `Steps.started_at` = now
- If `type` provided → `Steps.step_type` set
- If `agent_input` provided (SUB_AGENT) → `Steps.agent_input` set

**Common mistakes**:
- ❌ Trying to start a `COMPLETED` step (state machine rejects)
- ❌ Forgetting `agent_input` for `SUB_AGENT` steps (no error, but the dashboard panel will be empty)

**Test**: `tests/test_execute_ops.py::TestStartStep`

---

## 2. `complete-step.sh` — IN_PROGRESS → COMPLETED

**Input** (required: `step_id`):
```json
{"step_id": "...", "summary": "what was done in 1 sentence", "agent_output": "(SUB_AGENT only)"}
```

**Side effects**:
- `Steps.status` = `COMPLETED`
- `Steps.completed_at` = now
- If `summary` → `Steps.summary` set (distinct from `log_context`)
- If `agent_output` → `Steps.agent_output` set
- After this transition the step is **IMMUTABLE** — never call complete-step again on it

**Common mistakes**:
- ❌ Skipping `summary` (still works, but the dashboard shows blank — always provide one)
- ❌ Calling on a `PENDING` step (must `start-step` first; rejected by state machine)

**Test**: `tests/test_execute_ops.py::TestCompleteStep`

---

## 3. `fail-step.sh` — * → FAILED

**Input** (required: `step_id`):
```json
{"step_id": "...", "reason": "why it failed"}
```

**Side effects**:
- `Steps.status` = `FAILED`
- Reason appended to `Steps.log_context`

**Common mistakes**:
- ❌ Failing the step then trying to mutate it later (FAILED is terminal — same as COMPLETED)
- ❌ Vague reason ("it broke") — be specific so the audit trail is useful

**Test**: `tests/test_execute_ops.py::TestFailStep`

---

## 4. `append-log.sh` — telemetry, no transition

**Input** (required: `step_id`, `text`):
```json
{"step_id": "...", "text": "$ pytest -v\n=== 5 passed in 0.42s ==="}
```

**Side effects**:
- `text` appended to `Steps.log_context` (not overwritten)
- Telemetry truncates to last ~50 lines / ~1000 tokens automatically

**Common mistakes**:
- ❌ Putting curated summary in `text` — that goes in complete-step's `summary` field
- ❌ Calling repeatedly with the same text (it really does append; you'll get duplicates)

**Test**: `tests/test_execute_ops.py::TestAppendLog`

---

## 5. `deviate.sh` — INSERT sub-steps + revision_count++

**Input** (required: `parent_step_id`, `justification`, `sub_steps[]`):
```json
{
  "parent_step_id": "...",
  "justification": "why the plan needed to change",
  "sub_steps": ["TEST: ...", "CODE: ...", "COMMAND: ..."]
}
```

**Side effects**:
- New rows inserted into `Steps` with `parent_step_id` set + `depth_level = parent.depth + 1`
- `Plans.revision_count` += 1
- Justification logged in step log

**Circuit breakers** (script exits non-zero):
- `revision_count` would exceed `max_revisions` (default 5)
- `depth_level` would exceed 3

**Common mistakes**:
- ❌ Deviating to add a step that should have been planned upfront — fine occasionally, but if you're hitting `max_revisions=5` the original plan was wrong
- ❌ Picking a `COMPLETED` step as `parent_step_id` — rejected (exit 4: a COMPLETED step is immutable). Deviate on an IN_PROGRESS step, or on a FAILED one to recover it — a retry is a child of the attempt it retries (SKILL.md, *Retrying a failed attempt*)

**Test**: `tests/test_execute_ops.py::TestDeviate`

---

## 6. `record-skill.sh` — INSERT into SkillActivations

**Input** (required: `plan_id`, `name`, `source`):
```json
{
  "plan_id": "...",
  "name": "systematic-debugging",
  "source": "deferred-load",
  "step_id": "...",
  "reason": "test failure surfaced; activated debug skill"
}
```

**`source` enum** (4 values, enforced):
- `iron-law` — mandatory trigger
- `auto-search` — matched the task when you checked the available skills
- `explicit-mention` — user named it
- `deferred-load` — activated mid-flight (most common case for record-skill)

**Side effects**:
- New row in `SkillActivations`. Multiple rows for the same skill are allowed (each activation event is a separate audit row).

**Common mistakes**:
- ❌ Using record-skill for init-time skills — those go in `publish-plan.sh`'s `skills[]` array
- ❌ Using `iron-law` source for a deferred load — pick `deferred-load` (or `auto-search` if a search surfaced it at that moment)

**Test**: `tests/test_execute_ops.py::TestRecordSkill`

---

## 7. `finish-plan.sh` — close a plan by hand (rarely needed)

**Input** (required: `plan_id`):
```json
{"plan_id": "..."}
```

**Side effects**:
- Runs the deterministic review: when every regular step is terminal, the review step and the plan go COMPLETED or FAILED exactly as they would have on their own
- A legacy plan (no review step) or one with steps still PENDING is force-completed (`Plans.status` = `COMPLETED`)

**Common mistakes**:
- ❌ Calling it after the last step — the last regular step already closed the plan
- ❌ Calling it on a plan in NEEDS_REVIEW — refused (exit 7); drive the `<plan>-REVIEW.1` child instead

**Test**: `tests/test_execute_ops.py::TestFinishPlan`

---

## 8. `record-metric.sh` — INSERT into metrics (phase 7)

```json
{"step_id": "my-plan-D", "name": "mean_net_revenue", "value": 64.33, "unit": "usd"}
```

One NUMERIC observation. `project` defaults to the plan's `Plans.project`
(`plan_id`, or `step_id` → its plan); `value` must be a JSON number or a
strictly numeric string — `"high"`, `null`, `true`, `"nan"` exit non-zero and
write nothing. The metric outcome channel (`orchestrator/outcome_channels.py`)
later judges an expectation `metric:<name>` against the nearest row before
and after the expectation. `run-step.sh` records the same thing for you when
the input carries `"metrics_from_stdout": true` and the command prints
`metric name=<x> value=<v> [unit=<u>]` lines (source `run-step`).

Common mistakes: recording a metric the command did not print (write it where
it is measured, not where it is remembered); forgetting `project` on a plan
that has no attribution (exit non-zero: `'project' is required`).

## 9. `raise-budget.sh` — raise `Plans.max_revisions`, with the reason on the record

```json
{"plan_id": "my-plan-20260930120000", "new_max": 7, "reason": "two of the five revisions were coordinator corrections, not plan defects"}
```

The only way past the loop breaker. Until this existed, the breaker's own advice
("raise max_revisions") named an action nothing could perform, which left a
hand-written `UPDATE` as the last resort — the one thing this skill exists to
prevent.

**Side effects**:
- `Plans.max_revisions` = `new_max`
- one `Deviations` row (`target_step_id` NULL) reading `[BUDGET RAISED] max_revisions 5 -> 7: <reason>`
- `Plans.revision_count` is **untouched** — the raise must not spend the budget it grants

**Refused (exit non-zero, nothing written)**:
- a missing, empty or blank `reason` — the raise is a recorded decision, not a dial
- a `new_max` that is not an integer, or is not GREATER than the current ceiling (this flow only raises)
- an unknown `plan_id`

**Common mistakes**:
- ❌ Raising the ceiling because a step keeps failing. The breaker is usually right; the budget is for the case where the revisions were genuinely warranted (a coordinator's mid-flight correction, a recovery sub-step still to open) — and the reason you type is what a later reader judges that by.
- ❌ Editing `max_revisions` with SQL. Then the new ceiling has no reason attached and the plan's record does not show it moved.

**Test**: `tests/test_raise_budget.py`

## State machine summary

```
            ┌──── start-step ────┐                  ┌──── complete-step ───┐
PENDING ──→ │ STARTING / IN_PROG │ ── append-log ─→ │ IN_PROGRESS (looped) │ ──→ COMPLETED [terminal]
            └────────────────────┘                  └──────────────────────┘
                  │                                            │
                  └─────────── fail-step ──────────────────────┴──→ FAILED [terminal]
```

`deviate` doesn't transition the parent — it inserts new PENDING children. `record-skill`, `raise-budget` and `finish-plan` operate at plan-level.
