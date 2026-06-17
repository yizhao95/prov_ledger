# When to Use Which Op (Decision Tree)

Walk this tree at every executing-plans event. The right answer is always one of the 7 scripts.

```
EVENT: I'm about to start working on a step
  → bash scripts/start-step.sh {"step_id": "...", "type": "..."}

EVENT: A shell command/tool produced output worth keeping
  → bash scripts/append-log.sh {"step_id": "...", "text": "<raw output>"}

EVENT: The step finished successfully
  → bash scripts/complete-step.sh {"step_id": "...", "summary": "<one sentence>"}

EVENT: The step failed and I can't recover within this step
  → bash scripts/fail-step.sh {"step_id": "...", "reason": "<why>"}
  → THEN: brainstorm via systematic-debugging
  → THEN: bash scripts/deviate.sh to add retry sub-steps under a NEW step (NOT the failed one)

EVENT: I realized the parent step needs decomposition
  → bash scripts/deviate.sh {"parent_step_id": "...", "justification": "...", "sub_steps": [...]}

EVENT: I activated a NEW skill mid-flight (wasn't in publish-plan.sh's skills[])
  → bash scripts/record-skill.sh {"plan_id": "...", "name": "...", "source": "deferred-load"}

EVENT: All steps are COMPLETED — close the plan
  → bash scripts/finish-plan.sh {"plan_id": "..."}
```

## Anti-patterns (don't do these)

| ❌ Wrong | ✅ Right |
|---|---|
| `$PYBIN $CLI complete-step <id> --summary "..."` | `bash scripts/complete-step.sh input.json` |
| `sqlite3 ~/orchestrator.db "UPDATE Steps SET status='FAILED' WHERE..."` | `bash scripts/fail-step.sh input.json` |
| Edit the markdown plan file by hand | Markdown is regenerated; edits are lost. Use `regen-markdown` after script writes. |
| Skip `start-step.sh` and jump straight to `complete-step.sh` | State machine rejects PENDING→COMPLETED — start first |
| Use `append-log.sh` to record a curated summary | `complete-step.sh`'s `summary` field is for that. `append-log` is raw output only. |
| Call `record-skill.sh` for skills already in `publish-plan.sh`'s `skills[]` array | Those are recorded at init-time. record-skill is for **mid-flight** activations only. |
| Try to `complete-step.sh` an already-COMPLETED step "to update the summary" | Completed = immutable. Deviate to add a retry sub-step instead. |
| Call `finish-plan.sh` before all steps are COMPLETED | Finish all steps first; then finish the plan. |

## Quick reference: which script for which goal?

| Goal | Script |
|---|---|
| "Begin step X" | `start-step.sh` |
| "Save these test results in the audit trail" | `append-log.sh` |
| "Mark step X done" | `complete-step.sh` |
| "Mark step X failed and stop" | `fail-step.sh` |
| "Step X turned out bigger — break it down" | `deviate.sh` |
| "I just loaded a new skill I didn't anticipate" | `record-skill.sh` |
| "All done — close the plan" | `finish-plan.sh` |

## What this skill does NOT do

- Create plans → that's `writing-plans/scripts/publish-plan.sh`
- Read plans → use `$CLI list-plans` / `show-plan` / `list-skills` directly, or visit the dashboard
- Migrate the schema → that's `orchestrator/migrations/`
- Render the dashboard → that's `~/skill-workspace/orchestrator-webapp/`
