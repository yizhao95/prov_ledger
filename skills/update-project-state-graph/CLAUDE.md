# update-project-state-graph (review driver)

`scripts/review_run.py` runs the review when a plan touching a registered project reaches NEEDS_REVIEW:
lock the registered sha → `review_diff.full_verdict` gates (`contract_diff.py`) → `init_project.sh`
refresh → `--tests` → selfcheck → reason-slots/fill → complete-step.

- Writes only through `skills/executing-plans/scripts/*.sh`; reads `orchestrator.db` directly.
- Exit codes are a contract: 0 = plan COMPLETED, 1 = not closed, 5 = a write script failed or timed out,
  6 = bad input.
- Stdlib only; imports `selfcheck` from `../project-state-graph/scripts`. `graph_viz.py` is a byte copy
  of the analyzer's (pinned by `tests/test_graph_viz.py`).
- Tests: `bash scripts/run_tests.sh update-psg`. They need uv and a synced project-state-graph env
  (they run `init_project.sh`).
- The refresh and `--tests` read the working tree: don't edit the checkout until the review closes.
- On this repo the refresh takes ~13 min and `--tests` defaults to 600 s: run the suites yourself first,
  then pass a subset that fits (`--tests "bash scripts/run_tests.sh backend"`).
- Behaviour changes here usually shift project-state-graph scenario goldens; re-run those too.
