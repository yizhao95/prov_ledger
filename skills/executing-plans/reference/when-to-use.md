# When to Use Which Script — the Wrong Moves

Which script to run at each moment is decided in [`SKILL.md`](../SKILL.md): the cheat-sheet
table (one row per script, with *When*), the `run-step.sh` decision table, *Retrying a failed
attempt*, and the *NEEDS_REVIEW handoff*. This page does not repeat them. It keeps only the moves
that look reasonable and are wrong, each with the right one.

| ❌ Wrong | ✅ Right |
|---|---|
| Write to the DB by hand (`sqlite3 … "UPDATE Steps SET status='FAILED' …"`) or through the CLI | The matching script (`fail-step.sh`, …) — it validates the input and enforces the state machine |
| Run a shell command yourself, then `complete-step.sh` a `COMMAND` step | `run-step.sh` — `complete-step` refuses a `COMMAND` step without run-step's exit-code footer (exit 5) |
| Jump straight to `complete-step.sh` on a PENDING step | Start it first (`start-step.sh`), or let `run-step.sh` do both — the state machine rejects PENDING → COMPLETED |
| Put a curated summary in `append-log.sh`'s `text` | `summary` on `complete-step.sh`; `append-log` is for raw output only |
| `record-skill.sh` for a skill already in the plan-input's `skills[]` | Nothing — those were recorded at publish. `record-skill` is for **mid-flight** activations only |
| `complete-step.sh` on a COMPLETED step "to update the summary" | A COMPLETED step is immutable; put a follow-up sub-step on the next non-terminal step with `deviate.sh` |
| Retry a failed `D.1` as a sibling `D.2` | `deviate.sh` on `D.1`, giving `D.1.1` — a FAILED sibling outvotes a COMPLETED one for ever |
| `finish-plan.sh` after the last step | Nothing — the last regular step closes the plan. `finish-plan` is for legacy back-fill or force-finishing a partial plan, and is refused (exit 7) on a plan in NEEDS_REVIEW |
| `agent-review-close.sh` on a NEEDS_REVIEW plan that has a `<plan>-REVIEW.1` child | Drive the child: `start-step`, run the review, then `complete-step` or `fail-step` it — the plan closes from that |
| Ignore `needs_agent_review: true` on a tail line (`run-step`, `complete-step` or `fail-step`) | Start the review child step it names and dispatch the `update-project-state-graph` review |
