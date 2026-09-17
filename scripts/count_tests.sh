#!/usr/bin/env bash
# count_tests.sh — the one place that counts the tests, so the number in the
# docs is never a remembered one. Collection only: no test is executed.
set -uo pipefail
PY="${PYBIN:-$HOME/skill-workspace/.venv/bin/python}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 1
total=0
count() {                       # count <label> <path...>
    local label="$1"; shift
    local n
    n="$("$PY" -m pytest "$@" --collect-only -q -p no:cacheprovider 2>/dev/null | tail -1 | grep -oE '^[0-9]+' || echo 0)"
    printf '%-28s %5s\n' "$label" "$n"
    total=$((total + n))
}
count "orchestrator-backend" orchestrator-backend
count "orchestrator-webapp" orchestrator-webapp
count "writing-plans" skills/writing-plans/tests
count "executing-plans" skills/executing-plans
count "update-project-state-graph" skills/update-project-state-graph/scripts/tests
count "scripts" scripts/tests
count "examples" examples
(cd skills/project-state-graph/scripts && count "project-state-graph" tests)
printf '%-28s %5s\n' "TOTAL" "$total"
