---
name: executing-plans
description: "Use IMMEDIATELY after writing-plans, OR whenever a plan exists in ~/skill-workspace/orchestrator.db. Owns ALL post-publish writes to the orchestrator database via the 8 deterministic-flow scripts in scripts/: run-step (preferred for shell work), start-step, complete-step, fail-step, append-log, deviate, record-skill, finish-plan. Never bypass these scripts; never write to the DB ad-hoc. Triggers on every plan execution: code, SQL, schema, build, test, fix, implement, deploy, refactor, run, execute, continue, resume. When a test fails: never patch the test silently — fail-step it, root-cause via systematic-debugging, then deviate via scripts/deviate.sh."
---

# Executing Plans

The 9 deterministic write-flows (1 from writing-plans + 8 here) are **the only way** to mutate `~/skill-workspace/orchestrator.db`. Compose a small JSON input file, run the matching script, done. Never invoke the orchestrator CLI directly for writes; never construct ad-hoc SQL.

## 🔌 Boundary with `writing-plans`

`writing-plans` does ONE thing: composes a `plan-input.{json,yaml}` file and runs `~/.code_puppy/skills/writing-plans/scripts/publish-plan.sh` to insert the new plan. After that, **every** subsequent operation on that plan (start, log, complete, fail, deviate, record deferred-load skills, finish) is owned by THIS skill via the 7 scripts in `scripts/`.

If you find yourself wanting to "update the plan" — you ARE the update mechanism. You do not go back to writing-plans.

## 🛠️ The 9 deterministic flows (cheat sheet)

| Script | When | Required input | Optional input |
|---|---|---|---|
| `scripts/run-step.sh` ⭐  | **Preferred for COMMAND/CODE/TEST** — bundles start+exec+complete with auto-captured log_context (migration 006 + Aug 2026) | `step_id`, `type`, `command` | `summary`, `allow_nonzero` |
| `scripts/start-step.sh`    | Manual start — use for THINKING/DOCUMENTATION/ANALYSIS or when you need to inspect intermediate output | `step_id` | `type`, `agent_input`, `log_context` |
| `scripts/complete-step.sh` | Manual complete after non-shell work, OR after a `start-step` you split from exec | `step_id` | `summary`, `agent_output`, `log_context` |
| `scripts/fail-step.sh`     | Work failed and you can't recover in this step | `step_id` | `reason`, `log_context` |
| `scripts/append-log.sh`    | Captured raw shell/tool output worth keeping AFTER the fact | `step_id`, `text` | — |
| `scripts/deviate.sh`       | Realized the plan needs new sub-steps         | `parent_step_id`, `justification`, `sub_steps[]` | — |
| `scripts/record-skill.sh`  | Activated a NEW skill mid-flight              | `plan_id`, `name`, `source` | `step_id`, `reason` |
| `scripts/finish-plan.sh`   | **Usually auto** — manual only for back-fill of pre-2026-05-26 plans, or to force-finish a plan with PENDING steps | `plan_id` | — |
| `scripts/agent-review-close.sh` | **Only after a `needs_agent_review` handoff** — the review sub-agent's sole way to finalize a `NEEDS_REVIEW` plan | `plan_id`, `outcome` (`pass`\|`fail`) | `summary`, `log_context` |

### ⚡ `run-step.sh` — the preferred path for shell-based steps (Aug 2026)

Before Aug 2026, agents had to manually pass `log_context` on every `complete-step` to capture
stdout/stderr. A May 2026 audit found this was the #1 forgotten step — coverage had dropped
from 100% to 33%. `run-step.sh` eliminates the footgun the same way migration 006 eliminated
`finish-plan.sh`:

```json
{
  "step_id": "my-plan-A",
  "type":    "COMMAND",
  "command": "pytest tests/ -v"
}
```

```bash
bash scripts/run-step.sh /tmp/in.json
```

**What it does, atomically:**
1. Calls `start-step` with a `[run-step] kickoff <ts>` banner as initial `log_context`
2. Runs `bash -c "$command" 2>&1 | tee` — captures combined stdout+stderr (PIPESTATUS preserved)
3. Truncates output to ≤16 KiB (head 8 KiB + `--- TRUNCATED N BYTES ---` marker + tail 8 KiB)
4. Appends a `--- exit_code=N, runtime=Ns ---` footer
5. If exit 0 → calls `complete-step`; else → calls `fail-step`
6. Exits with the wrapped command's true exit code (so the calling agent sees pass/fail naturally)
7. The auto-review trigger from migration 006 still fires — plans still auto-close

**Decision table for which entry point to use:**

| Situation | Use |
|---|---|
| Step IS a shell command / test / build / migration  | `run-step.sh` ⭐ |
| Step IS pure thinking / docs / analysis (no shell)  | `start-step.sh` + `complete-step.sh` |
| Multi-command shell work in one step                | `run-step.sh` with `"command": "cmd1 && cmd2"` or a heredoc |
| You need to inspect intermediate output before committing the step result | `start-step.sh`, then `complete-step.sh` with explicit `log_context` |


### 🤖 Auto-review-and-complete (migration 006 — May 2026)

Every newly-published plan now gets one extra terminal step with `is_review=1`
(step_id = `<plan>-REVIEW`). This step is flipped by a **deterministic Python
procedure** (`api.review_and_complete`), NOT by the LLM agent.

**The procedure auto-fires from `complete-step.sh` and `fail-step.sh`** any time
a non-review step transitions. Decision tree:

| Sibling state | Review step | Plan |
|---|---|---|
| Any PENDING / IN_PROGRESS | stays PENDING | stays IN_PROGRESS |
| All COMPLETED | → COMPLETED | → COMPLETED |
| All COMPLETED **and the plan mentions a registered project** | → **NEEDS_REVIEW** + a tracked child step `<plan>-REVIEW.1` is created | stays IN_PROGRESS (awaits agent) |
| All FAILED steps have **recovered** deviation sub-trees (rest terminal) | → COMPLETED | → COMPLETED |
| Any FAILED step is **unrecovered** (rest terminal) | → FAILED | → FAILED |

**Recovery semantics (Jun 2026):** a FAILED step is considered *recovered* when
every direct non-review child is itself recovered — recursively. So if step `D`
failed and you `deviate.sh`'d into `D.1` which COMPLETED, the plan no longer
fails because of `D`. A FAILED leaf step (no deviation children) is never
recovered and still poisons the plan. Recursion depth is bounded by the
`depth_level<=3` circuit breaker, so this can't pathologically deep-walk.

**Implications for the agent:**
- You almost never need to call `finish-plan.sh` anymore. The last `complete-step`
  on the last regular step closes the plan automatically.
- The auto-trigger ignores the review step itself (no recursion risk).
- For legacy plans without a review row (pre-006), `finish-plan.sh` falls back
  to the old unconditional COMPLETED behavior — your back-fill workflow is preserved.
- For a partial plan you want to force-finish (some steps still PENDING),
  `finish-plan.sh` also falls back to plain `complete_plan` as an escape hatch.

### 🤖 NEEDS_REVIEW handoff (registered-project plans)

When a plan mentions a **registered project** (one in
`~/skill-workspace/project-graphs/projects.json`, variance-tolerant match), the
deterministic procedure does **not** auto-close it. Instead it (1) flips the
review step to `NEEDS_REVIEW`, (2) bumps the plan revision, and (3) inserts a
**tracked child step** `<plan>-REVIEW.1` (type `SUB_AGENT`, status `PENDING`)
under the review step. The plan stays `IN_PROGRESS`, and the
`complete-step`/`fail-step` tail line carries:

```json
{"ok":true,"op":"complete-step",...,"needs_agent_review":true,"project":"<name>","review_step_id":"<plan>-REVIEW","review_child_step_id":"<plan>-REVIEW.1"}
```

**When you see `needs_agent_review: true`, you MUST:**
1. `start-step` the child step `review_child_step_id` (`<plan>-REVIEW.1`).
2. Invoke the `code-puppy` sub-agent with the **`update-project-state-graph`**
   skill, passing `plan_id`, `project`, and the child step id.
3. When the sub-agent finishes, finalize **the child step**:
   `complete-step` it on a clean review, or `fail-step` it on a gap. The plan
   then **auto-closes from the child outcome** — child COMPLETED → review +
   plan COMPLETED; child FAILED → review + plan FAILED. No extra close call.

**Do NOT** close the plan or the review step directly — drive the tracked child
step and let the deterministic procedure finalize. `agent-review-close.sh`
remains a documented **fallback** for plans without a child (e.g. legacy
in-flight plans) and keeps any child in sync. Plans mentioning no registered
project are unaffected and still auto-close as above.

Schema: [`update-input.schema.json`](update-input.schema.json) (oneOf, one per op).
Examples: [`update-input.example.json`](update-input.example.json) (9 worked examples).

### Invocation pattern (identical for every script)

```bash
# 1. compose input
cat > /tmp/in.json <<'EOF'
{"step_id": "deep-health-20260514153045-A", "type": "ANALYSIS"}
EOF

# 2. run script
bash ~/.code_puppy/skills/executing-plans/scripts/start-step.sh /tmp/in.json
```

The script:
- Validates required fields (clear error naming the offending field)
- Enforces state machine + circuit breakers via `orchestrator.api`
- Writes to `${ORCH_DB:-~/skill-workspace/orchestrator.db}`
- Prints the result as JSON to stdout, **followed by a single-line OK marker** as the final stdout line: `{"ok":true,"op":"<op>","step_id":"<id>"}`
- Exits non-zero on any validation/state failure (with `❌ apply_op: ...` to stderr)

### 🛡️ Safe invocation pattern (Bug 1 fix — read this!)

**⚠️ NEVER pipe through `| tail -1` without `2>&1` and an exit-code check.** Bash's pipefail in the script does NOT propagate to your outer pipeline; failures will be silently swallowed and your plan will desync from reality.

Use one of these instead:

```bash
# (a) Easiest — capture, then check the OK marker on the final line:
OUT=$(bash scripts/complete-step.sh /tmp/in.json) || { echo "FAILED: $OUT" >&2; exit 1; }
echo "$OUT" | tail -1 | python3 -c 'import json,sys; assert json.loads(sys.stdin.read())["ok"]'

# (b) If you really must pipe, redirect stderr too + use PIPESTATUS:
bash scripts/complete-step.sh /tmp/in.json 2>&1 | tail -1
[[ ${PIPESTATUS[0]} -eq 0 ]] || { echo "step write failed"; exit 1; }
```

## 📷 MANDATORY: capture raw output via `log_context` (Bug 2 fix)

For every COMMAND or CODE step, the raw shell/tool/test output **MUST** end up in `Steps.log_context` — either:

- **(preferred)** inline: pass `log_context: "<raw output>"` to `complete-step.sh` (or `start-step.sh` / `fail-step.sh`); OR
- **(fallback)** call `append-log.sh` separately before `complete-step.sh`.

A non-trivial step with `log_context = ""` is a puppy failure — the dashboard's log panel goes blank and post-hoc debugging becomes impossible. The two failed plans `tree-readme-pr-20260514170701` (0/13 steps with logs) and `rebase-onto-main-20260514182554` (0/11 steps with logs) are the cautionary examples.

## 🚨 Mandatory rules

| Rule | Why |
|---|---|
| Use ONLY the 7 scripts for writes — never call `orchestrator-cli.py <verb>` directly | Eliminates flag-typo class of bugs (we hit `complete-plan --reason` last week) |
| `summary` is ONE human-curated sentence; `text` (in append-log) is raw machine output | Telemetry vs. curation are distinct fields |
| Once a step is COMPLETED, it is IMMUTABLE — to redo work, deviate + add a retry sub-step | Audit trail |
| `revision_count` ≤ `max_revisions` (default 5) — circuit breaker | Prevents thrash |
| `depth_level` ≤ 3 — circuit breaker | Keeps plans auditable |
| For `SUB_AGENT` steps: capture both `agent_input` (in start-step) and `agent_output` (in complete-step) | The dashboard renders these in dedicated panels |
| For COMMAND/CODE steps: capture raw output via `log_context` (inline on complete-step, OR separate append-log call) — NOT optional in practice | The dashboard's log panel goes blank otherwise; post-hoc debugging fails |
| Never pipe through `\| tail -1` without `2>&1` + PIPESTATUS check — the wrapper's pipefail does NOT propagate to your outer shell | Silent failures will desync the plan from reality (the F-stuck-PENDING bug) |

## 👀 Reads stay direct

These don't mutate, so you can call them however:

```bash
PYBIN=~/skill-workspace/orchestrator/.venv/bin/python
CLI=~/skill-workspace/orchestrator-cli.py
$PYBIN $CLI list-plans
$PYBIN $CLI show-plan <plan_id>
$PYBIN $CLI list-skills <plan_id>
sqlite3 ~/skill-workspace/orchestrator.db "SELECT * FROM Plans WHERE status='IN_PROGRESS';"
```

The dashboard at http://localhost:8765 is the visual equivalent.

## 📚 Reference index

- [`reference/op-catalog.md`](reference/op-catalog.md) — full per-op reference (state transitions + common mistakes)
- [`reference/when-to-use.md`](reference/when-to-use.md) — decision tree at every executing-plans event
- [`update-input.schema.json`](update-input.schema.json) — formal schema (oneOf per op)
- [`update-input.example.json`](update-input.example.json) — copy-paste-edit examples

## 🧪 Tests

```bash
~/skill-workspace/orchestrator/.venv/bin/python -m pytest \
  ~/.code_puppy/skills/executing-plans/tests/ -v
```

17 tests: 16 per-op (happy + validation + state-machine + circuit breakers) + 1 full lifecycle smoke that walks the entire publish→finish cycle through the documented scripts.

## 🔗 See also

- [`../README.md`](../README.md) — repo-level overview, installation, activation paths, full skill catalog
- [`../writing-plans/SKILL.md`](../writing-plans/SKILL.md) — the other half of the contract; owns the initial publish flow
- **Orchestration dashboard** at `~/skill-workspace/orchestrator-webapp/` — tree view + parallel branches. See README → "Recommended companion" for setup.
