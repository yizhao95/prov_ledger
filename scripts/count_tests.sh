#!/usr/bin/env bash
# count_tests.sh — the one place that counts the tests, so the number in the
# docs is never a remembered one. Collection only: no test is executed.
# The project-state-graph suite is collected from its own directory, because
# that is how it is run.
set -uo pipefail
PY="${PYBIN:-$HOME/skill-workspace/.venv/bin/python}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
total=0

collected() {                   # collected <dir-to-run-from> <path...>
    local from="$1"; shift
    # pytest prints either "N tests collected" or "N/M tests collected (K deselected)"
    (cd "$from" && "$PY" -m pytest "$@" --collect-only -q -p no:cacheprovider 2>/dev/null) \
        | grep -oE '[0-9]+(/[0-9]+)? tests? collected' | tail -1 | grep -oE '^[0-9]+'
}

count() {                       # count <label> <dir-to-run-from> <path...>
    local label="$1" from="$2"; shift 2
    local n; n="$(collected "$from" "$@")"; n="${n:-0}"
    printf '%-28s %5s\n' "$label" "$n"
    total=$((total + n))
}

count "orchestrator-backend"        "$ROOT" orchestrator-backend
count "orchestrator-webapp"         "$ROOT" orchestrator-webapp
count "writing-plans"               "$ROOT" skills/writing-plans/tests
count "executing-plans"             "$ROOT" skills/executing-plans
count "update-project-state-graph"  "$ROOT" skills/update-project-state-graph/scripts/tests
count "scripts"                     "$ROOT" scripts/tests
count "examples"                    "$ROOT" examples
count "project-state-graph"         "$ROOT/skills/project-state-graph/scripts" tests
printf '%-28s %5s\n' "TOTAL" "$total"
