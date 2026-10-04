# suites.sh — the one list of test suites. Sourced, never executed.
#
# Each entry is  name|directory to run from (relative to the repo root)|pytest target.
# Every suite runs in its own pytest process: each sets its import path from its
# own directory, and a combined run moves the rootdir and the conftest order, so
# it reports failures a separate run does not have.
#
# Read by scripts/run_tests.sh (run, count, list) and by the release check
# (scripts/release_e2e/stage1_install.sh). Add a suite here and nowhere else.
SUITES=(
    "scripts|.|scripts/tests"
    "plugin|.|tests"
    "backend|.|orchestrator-backend"
    "webapp|.|orchestrator-webapp"
    "writing-plans|.|skills/writing-plans/tests"
    "executing-plans|.|skills/executing-plans"
    "update-psg|.|skills/update-project-state-graph/scripts/tests"
    "examples|.|examples"
    "psg|skills/project-state-graph/scripts|tests"
)
