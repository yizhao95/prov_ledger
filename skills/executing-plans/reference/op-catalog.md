# Op Catalog — Full Reference

Per-op deep-dive: input shape, state transition, output JSON, common mistakes, link to test case.

All scripts share the same shape:
```bash
bash ~/.code_puppy/skills/executing-plans/scripts/<op>.sh path/to/input.json
```
Env: `ORCH_DB` overrides the SQLite path (default `~/skill-workspace/orchestrator.db`).
Output: pretty-printed JSON to stdout. Errors: `❌ apply_op: <msg>` to stderr, non-zero exit.

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
- ❌ Picking a `COMPLETED` step as `parent_step_id` — only IN_PROGRESS parents make sense

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
- `auto-search` — surfaced by `list_or_search_skills`
- `explicit-mention` — user named it
- `deferred-load` — activated mid-flight (most common case for record-skill)

**Side effects**:
- New row in `SkillActivations`. Multiple rows for the same skill are allowed (each activation event is a separate audit row).

**Common mistakes**:
- ❌ Using record-skill for init-time skills — those go in `publish-plan.sh`'s `skills[]` array
- ❌ Using `iron-law` source for a deferred load — pick `deferred-load` (or `auto-search` if a search surfaced it at that moment)

**Test**: `tests/test_execute_ops.py::TestRecordSkill`

---

## 7. `finish-plan.sh` — IN_PROGRESS → COMPLETED at the plan level

**Input** (required: `plan_id`):
```json
{"plan_id": "..."}
```

**Side effects**:
- `Plans.status` = `COMPLETED`

**Common mistakes**:
- ❌ Calling on an already-COMPLETED plan (rejected — idempotent failure)
- ❌ Calling before all steps are completed (no error today, but leaves the plan in a confusing state — finish all steps first)

**Test**: `tests/test_execute_ops.py::TestFinishPlan`

---

## State machine summary

```
            ┌──── start-step ────┐                  ┌──── complete-step ───┐
PENDING ──→ │ STARTING / IN_PROG │ ── append-log ─→ │ IN_PROGRESS (looped) │ ──→ COMPLETED [terminal]
            └────────────────────┘                  └──────────────────────┘
                  │                                            │
                  └─────────── fail-step ──────────────────────┴──→ FAILED [terminal]
```

`deviate` doesn't transition the parent — it inserts new PENDING children. `record-skill` and `finish-plan` operate at plan-level.
