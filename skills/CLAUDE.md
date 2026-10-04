# skills/

Shipped plugin content: everything here reaches plugin users. A SKILL.md is product prose for
their agents, not instructions for you.

| skill | code | origin |
|---|---|---|
| writing-plans, executing-plans | `scripts/` + tests | adapted from obra/superpowers |
| brainstorming, test-driven-development, systematic-debugging, subagent-driven-development | prompt only (brainstorming has a Node helper) | local variants of superpowers skills |
| ledger, receipts | prompt only; drive the `provledger` CLI (`orchestrator/ask/`, `why.py`) | original |
| project-state-graph, update-project-state-graph | see their own CLAUDE.md | original |

## Plan scripts (writing-plans/scripts, executing-plans/scripts)
- `.sh` wrappers resolve Python inline: `PYBIN` → `${PROVLEDGER_VENV:-~/skill-workspace/.venv}` → … →
  `python3` (pinned by `tests/test_resolver_wiring.py`). `.py` helpers put `orchestrator-backend` on
  `sys.path` and `import orchestrator`.
- DB is `$ORCH_DB`, default the real `~/skill-workspace/orchestrator.db`; every open migrates.
  Never run a script by hand without `ORCH_DB` and `PSG_REGISTRY_PATH` pointing at temp files.
- `*.schema.json` are documentation only. Real validation: `publish_plan.py::_validate`,
  `_apply_op.py::_require`; `test_update_input_schema.py` / `test_plan_input_schema.py` hold the
  schemas to that code. Change script I/O, schema, example, SKILL.md and `reference/` together.
- Contract consumers: executing-plans tests call `publish-plan.sh`; `update-project-state-graph`'s
  `review_run.py` calls executing-plans scripts. `ledger_store.constraints_for` reads `LedgerEntries`,
  while the backend's `constraints.anchored_constraints` reads `change_reason_v`.
- `publish-plan.sh` prints indented multi-line JSON (warnings on stderr). If a publish looked failed,
  query `Plans` before publishing again: a second publish creates a second plan.

## Tests
`bash scripts/run_tests.sh writing-plans executing-plans plugin` (`plugin` = root `tests/`: manifests,
bundled skills, SKILL.md phrase pins).

## Editing SKILL.md
- Frontmatter is `name` + `description`; the description is the trigger. Over 1024 chars it is
  truncated in the skill list (`tests/test_skill_descriptions.py` enforces the limit).
- ledger/receipts wording is pinned by `tests/test_skill_bundle.py` and `tests/test_receipts_skill.py`.
