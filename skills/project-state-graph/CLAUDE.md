# project-state-graph (analyzer)

Builds `<name>-state-graph.db` (SQLite code/data graph + append-only node history) and
`ARCHITECTURE.md`, and registers the project in `projects.json`.

## Layout (`scripts/`)
- `analyzer/`: `python -m analyzer` (`cli.py`: build | history | backfill | ambiguities | calibration |
  arbiter-eval). `store.py` is the only DB writer and owns the schema; `history.py` does
  snapshot → match → node_key/events; every other module is one stage, order fixed in `cli.run`.
- `analyzer/_host.py` is the only bridge to `orchestrator-backend` (`test_host_import`).
- `selfcheck.py`: stdlib invariants; exit 1 only on error severity.
- `init_project.sh`: analyzer → architecture_md → registry → selfcheck → viz_slices.
- `tests/scenarios/`: whole-plugin timeline goldens. `tests/corpus/`: re-exports of `provledger.testing`.

## Environment
- `scripts/` is its own uv project (Python ≥ 3.13, `provledger` editable from `orchestrator-backend`).
  `init_project.sh` uses `uv run`; re-run `uv sync` after backend changes.
- Tests use `PY=~/skill-workspace/.venv/bin/python`.

## Tests
- all, ~8 min: `bash scripts/run_tests.sh psg` (from repo root). Segments, from `scripts/`:
- scenarios: `$PY -m pytest tests/test_scenarios.py tests/test_scenario_runner.py -q`
- corpus: `$PY -m pytest tests/test_corpus_*.py -q`
- rest: `$PY -m pytest tests -q --ignore=tests/test_scenarios.py --ignore=tests/test_scenario_runner.py --ignore-glob='tests/test_corpus_*.py'`
- outside pytest: `bash tests/test_cold_archive.sh`. Opt-in LLM test: `-m llm_consistency`.

## Rules
- Scenarios run the whole plugin (publish-plan → run-step → review_run.py → backend); changes there can
  break goldens too. Regenerate only for intended event changes:
  `PROVLEDGER_UPDATE_GOLDEN=1 $PY -m pytest tests/test_scenarios.py -k NAME`, rerun without it, review the diff.
  Every `scenario.toml` needs `must_not`.
- `node_snapshot`/`node_event` are append-only; a node_key is assigned once.
- Changing `store._HISTORY_SCHEMA` → copy it verbatim into `orchestrator-backend/tests/_psg_schema.py`.
  `TOOL_VERSION` tracks the release version.
- `analyzer backfill` on a sub-project of a larger repo needs `--subdir DIR`, or every replayed commit
  analyses the whole repo. `analyzer arbiter-eval` exits 0 whatever the gate says: read its `gate:` line.
- `analyzer/graph_viz.py` is byte-copied to `update-project-state-graph/scripts/`; that suite's
  `test_graph_viz.py` fails when they differ. Edit the analyzer's, then copy it over.
- `cli.run`, `selfcheck` and `init_project.sh` read or write `~/skill-workspace` unless `ORCH_DB` and
  `PSG_REGISTRY_ROOT` point elsewhere (`PSG_REGISTRY_PATH` alone also works: its directory becomes
  the registry root).
