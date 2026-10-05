# orchestrator-backend: the `provledger` package

Stdlib-only SQLite core. Owns the orchestrator DB: the plan/step state machine the plan skills drive,
and the provenance ledger (utterances, references, reasons, constraints, outcomes) that the hooks feed
and the CLI, /ledger, /receipts and the dashboard read.

`orchestrator/` ships as `provledger` (`package-dir` map in pyproject; a new subpackage needs an entry).
Skills, hooks and tests import `orchestrator`; the webapp and PSG analyzer import `provledger`.
Inside the package: relative imports only.

## Commands (`PY=.venv/bin/python`, the repo's dev venv)
- Tests: `bash scripts/run_tests.sh backend`, or one file: `$PY -m pytest orchestrator-backend/tests/test_x.py -q`
- `orchestrator` is not importable from the venv outside pytest: use `PYTHONPATH=orchestrator-backend`.
  CLI: `python -m orchestrator.cli <cmd>`. Hook: `echo '<json>' | python -m orchestrator.hooks <Event>`.
- Markers: `live` reads the real ledger and is deselected (`PROVLEDGER_LIVE=1 … -m live`, by hand only).
  Never skip `veto` tests.
- No test may call a model: inject a stub `runner`. `--runner claude`, `significance eval` and
  `trigger eval` spawn `claude -p`; manual only.

## Migrations (`orchestrator/migrations/NNN_name.sql`)
- Applied on every DB open, tracked by filename in `schema_version`. A file that has run anywhere is
  frozen: add the next number, never edit or grow an existing one. Add `tests/test_migration_NNN.py`.
- Changing a CHECK means a table rebuild (`_new` → INSERT…SELECT → DROP → RENAME; see 015/019/021/030),
  then recreate indexes, append-only triggers and dependent views. Rebuild a view from its latest body.
- PRAGMAs at a file's head or tail run outside the savepoint.

## Invariants
- Provenance and log tables are append-only by trigger. The only UPDATEs: `change_reason.superseded_by`,
  `reference.last_checked`, the `declared_node`/`artifact_file` whitelists. Mutable: Plans, Steps
  (COMPLETED is frozen), LedgerEntries, session_run.
- Hash-chained tables (utterance, reference, change_reason, reference_check, declared_node, occurrence):
  insert only through module helpers. New column on one → extend `provenance._PRE_COLUMN`.
- A reason's tier is derived from what it points at; bare text never becomes `stated`. Timestamps come
  from the DB clock. Step status changes only via `api`/`state_machine`.
- Compound writes: helpers with `commit=False` inside `db.transaction(conn)`.
- `triggers.evaluate` skips nodes whose events are all `PASSIVE_EVENTS` (matched / identity only): no rule
  writes a reason on a node that was only touched.
- Hooks always exit 0 and log errors to `$PROVLEDGER_HOOK_ERRORS`; keep the `__main__` guard last in `hooks.py`.
- `testing/` is runtime code shipped in the wheel (claude_arbiter, source_words, prompts/), not just helpers.

## Gotchas
- Defaults hit real data: `ORCH_DB`, `PSG_REGISTRY_PATH`, `~/skill-workspace/provledger-extensions.json`.
  There is no autouse isolation; monkeypatch them in tests.
- A plan close writes `git notes --ref provledger` on the project repo.
- Readers use `change_reason(_v)`: seed tests with `provenance.insert_reason` /
  `constraints.record_constraint`, never `node_reason` or `LedgerEntries` (only the skills'
  `ledger_store.constraints_for` still reads `LedgerEntries`).
- Append-only rows can't be fixed afterwards: pass timestamps at insert.
- A module reached as a package attribute (`provledger.testing.calibration`) must be imported in the
  package `__init__`; in-process tests hide the miss, a fresh subprocess does not.
- Close, outcome and reason changes shift the PSG scenario goldens: run `bash scripts/run_tests.sh psg`.
  Never put a timestamp or id into an outcome's reason or value.
- Headless `claude -p`: never pass `--bare` (it skips the keychain: "Not logged in").
- Hooks load at session start: a session that installed or updated the plugin, or continues one that
  did, writes no hook rows. To watch a hook fire, start a new headless `claude -p` with `ORCH_DB`,
  `PSG_REGISTRY_PATH` and `PSG_REGISTRY_ROOT` on scratch paths.
- `tests/_psg_schema.py` must stay a verbatim copy of the analyzer's `store._HISTORY_SCHEMA`.
- Other suites depend on internals: executing-plans calls private `api._is_step_recovered`; the analyzer
  uses `graph_api`/`providers`/`extensions`/`declared`; the webapp reads the tables and views. Run them
  when changing those.
- `provledger ask card` without `--out` writes `evidence-card-<id>.md` into cwd.
