---
name: writing-plans
description: "ALWAYS loaded FIRST when starting any new task — non-negotiable, before any tool use, regardless of whether the task seems trivial. Triggers on EVERY user turn: questions, requests, code, debugging, design, build, create, make, write, fix, add, change, set up, plan, implement, refactor, install, deploy, schema, SQL, database, YAML, JSON, config, API, function, class, test, document, hello, help me. Workflow: (1) If other skills are needed for this task (domain, language, framework, integration), load those FIRST via list_or_search_skills + activate_skill — do NOT proceed until all required skills are loaded. (2) Once all needed skills are active, evaluate the task scope: count anticipated steps and check whether invoke_agent will be called. (3) GATING RULE: trigger the full writing-plans body (compose plan-input.json + run scripts/publish-plan.sh) IF AND ONLY IF estimated_steps >= 3 OR invoke_agent will be called. Otherwise note 'trivial — no plan needed' and proceed directly. (4) Whenever a plan IS published, IMMEDIATELY trigger the executing-plans skill — never defer it, never skip it. The chain writing-plans → executing-plans is atomic."
---

# Writing Plans

SQLite-backed plan creation. The agent **composes** a plan-input file (JSON or YAML) and **publishes** it via `scripts/publish-plan.sh`. After publish, all revisions/updates/deviations belong to the **executing-plans** skill — this skill never modifies an already-published plan.

## ⚡ When to use this skill

Trigger the full workflow when:
- `estimated_steps >= 3`, **OR**
- `invoke_agent` will be called

Otherwise: announce "trivial — no plan needed" and proceed directly. (Trivial 1-step tasks may still publish for audit trail.)

## 🔄 The 5-step workflow

1. **Load other skills first.** Call `list_or_search_skills` for the task domain + run the standing-skill checklist. Activate every skill that even might apply. Do NOT proceed until they're all loaded.

2. **Ensure the dashboard is up.** Run the idempotent script — never read the underlying webapp launcher:
   ```bash
   bash ~/.code_puppy/skills/writing-plans/scripts/ensure-dashboard.sh
   ```

3. **Draft `plan-input.json`** (or `.yaml`). Schema: `plan-input.schema.json`. Worked example: `plan-input.example.json`. Required fields: `goal`, `prefix`, `steps[]`. Strongly recommended: `user_query` (verbatim), `skills[]` (every iron-law + topic skill activated).

4. **Self-review** (5-bullet checklist — see `reference/step-quality.md`):
   - [ ] Spec coverage — every requirement maps to a step
   - [ ] No placeholders ("TBD", "handle errors", "similar to step N")
   - [ ] TDD pairing — every `CODE` step preceded by a `TEST` step (`reference/tdd-test-spec.md`)
   - [ ] Skills declared — every iron-law + topic skill in `skills[]`
   - [ ] 10k rule respected — any step >10k context tagged `SUB_AGENT` with `ctx_estimate_k` (`reference/10k-context-rule.md`)

5. **Publish to SQLite** — this skill's only side effect on the database:
   ```bash
   bash ~/.code_puppy/skills/writing-plans/scripts/publish-plan.sh path/to/plan-input.json
   ```
   The script returns `{plan_id, step_ids, review_step_id, skills_recorded}` as JSON.
   `review_step_id` is the auto-appended `<plan>-REVIEW` step (migration 006). It
   flips to COMPLETED or FAILED automatically when the last regular step terminates
   — no `finish-plan.sh` call needed. See `executing-plans/SKILL.md` for details.

6. **Hand off to `executing-plans`** — immediately, atomically. No pause, no user confirmation. Anything that happens to the plan after this moment (revise, update, modify, deviate, retry, decompose) is **executing-plans** territory.

## 🚨 Mandatory rules

| Rule | Where it's enforced |
|---|---|
| Every `CODE` step preceded by a `TEST` step describing behavior + edge cases + assertions | `reference/tdd-test-spec.md` |
| Every step description names WHO/WHAT/HOW (no placeholders) | `reference/step-quality.md` |
| Every activated skill recorded in `skills[]` with one of 4 valid sources | `reference/skill-source-enum.md` |
| Steps >10k context tagged `SUB_AGENT` with `ctx_estimate_k` and `[parallel-safe]` when applicable | `reference/10k-context-rule.md` |
| When `project` is set, `declared_targets` is MANDATORY (publish errors out if missing) | `scripts/publish_plan.py` (Phase D) |

## 🛰️ Plan-time pre-flight (provLedger Phase D)

When a plan-input names a tracked **`project`** (one in
`~/skill-workspace/project-graphs/projects.json`), publishing becomes one atomic
act with **forward impact analysis** — you cannot publish a project-scoped plan
without declaring what it touches:

- Add `"project": "<name>"` and `"declared_targets": ["<symbol>", ...]` to the
  plan-input. `declared_targets` is the symbols/columns you judge the change will
  touch (qualified_name or bare name).
- `publish_plan.py` then runs `impact_preflight.compute_impact_context` against
  that project's state-graph BEFORE the Plan row is inserted, and stores the
  result on `Plans.impact_context`. Missing `declared_targets` → **non-zero exit**.
- The analysis unions your declared targets with a deterministic keyword
  reverse-lookup of `user_query`, then per symbol: **existing** → callers /
  output_consumers / dtype_map / lineage_downstream; **missing** → flagged
  `new` (a useful signal: genuinely new, or a misremembered name). It also
  surfaces **upstream-data assumptions** (sql_table/api_source assumed schema +
  a fail-fast load-seam recommendation) and ledger reminders (Phase E — see below).
- **Capability boundary:** strongest for *modifying existing code*. For brand-new
  modules it degrades to describing the existing functions the new code will call
  — the graph only describes code that exists.
- Project-less plans skip all of this and publish exactly as before.

## 📓 Decision-Memory Ledger (provLedger Phase E)

The **ledger track** — the home for everything the gate track honestly *cannot*
check: semantic contracts, data-engineering rationale, and past failures. It is
the "never repeat a mistake" half of provLedger.

- The ledger stores **decisions** (+ their rationale) and **anti-patterns**
  (failures + their cause), scoped to a project, with subject symbols/tables +
  free keywords for matching.
- It is **manual and opt-in** — you add entries by hand as real decisions get
  made and real failures get hit (never auto-populated):
  ```
  bash scripts/ledger-add.sh add --project <name> --kind decision \
      --statement "rolling-window split, not random split" \
      --rationale "random split leaks temporal information" \
      --subjects train_test_split,split --keywords split,rolling,temporal
  bash scripts/ledger-add.sh list --project <name>
  ```
- At plan time (the Phase D step-4 match point), a **deterministic lexical**
  fuzzy-match scores active entries against the plan's declared_targets +
  user_query and surfaces the top matches as `impact_context.ledger_reminders`.
  A decision → `reminder: <statement> — because <rationale>; confirm before
  changing.` An anti-pattern → `warning: <statement> was tried and failed —
  <rationale>; reconsider before proceeding.`
- **Reminders are advisory — NEVER blocks.** A plan that triggers a reminder
  still publishes (exit 0); the agent is simply reminded *why* the current
  choice exists.
- **Honest boundary:** matching is lexical/deterministic (no embeddings/LLM), so
  recall is bounded by the keywords a human recorded — the ledger is only as good
  as what gets written into it. That is by design: grow it gradually from real
  decisions and real failures.
## 📚 Reference index

- [`reference/step-types.md`](reference/step-types.md) — the 6 valid step types (THINKING / ANALYSIS / CODE / COMMAND / DOCUMENTATION / SUB_AGENT)
- [`reference/skill-source-enum.md`](reference/skill-source-enum.md) — the 4 valid skill sources + always-on iron-laws
- [`reference/10k-context-rule.md`](reference/10k-context-rule.md) — when to dispatch a SUB_AGENT
- [`reference/step-quality.md`](reference/step-quality.md) — specificity bar, granularity, no-placeholders
- [`reference/tdd-test-spec.md`](reference/tdd-test-spec.md) — what every TEST step description must contain
- [`plan-input.schema.json`](plan-input.schema.json) — formal schema for the input file
- [`plan-input.example.json`](plan-input.example.json) — copy-paste-edit starting point
- [`plan-document-reviewer-prompt.md`](plan-document-reviewer-prompt.md) — optional reviewer agent prompt

## 🔌 Boundary with executing-plans

This skill **only** writes the plan to SQLite. It does NOT:
- modify a plan after publish
- record `record-skill` (deferred-load activations) — that's executing-plans
- run `start-step` / `complete-step` / `evaluate --deviation` — that's executing-plans
- enforce circuit breakers (immutability, max_revisions, depth_limit) at runtime — that's executing-plans

If you find yourself wanting to "update the plan I just wrote", stop. The next call belongs to executing-plans.

## 🛠️ Tests

```bash
~/skill-workspace/orchestrator/.venv/bin/python -m pytest \
  ~/.code_puppy/skills/writing-plans/tests/ -v
```

10 tests cover both scripts (publish-plan: 7, ensure-dashboard: 3).

## 🔗 See also

- [`../README.md`](../README.md) — repo-level overview, installation, activation paths, full skill catalog
- [`../executing-plans/SKILL.md`](../executing-plans/SKILL.md) — the other half of the contract; owns ALL post-publish writes
- **Orchestration dashboard** at `~/skill-workspace/orchestrator-webapp/` — visualizes plans (tree view + parallel branches). `publish-plan.sh` auto-launches it. See README → "Recommended companion" for setup.
