#!/usr/bin/env bash
# finish-plan.sh — deterministic flow for finish-plan write op.
# Composes input as JSON/YAML, this script validates + writes via the orchestrator.
# See ~/.code_puppy/skills/executing-plans/update-input.example.json for the input shape.
#
# Env: ORCH_DB overrides the SQLite path (default: ~/skill-workspace/orchestrator.db).
set -uo pipefail
if [[ $# -ne 1 ]]; then
    echo "usage: finish-plan.sh <input.json|yaml|yml>" >&2
    exit 2
fi
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYBIN="${PYBIN:-${HOME}/skill-workspace/orchestrator/.venv/bin/python}"
exec "${PYBIN}" "${SCRIPT_DIR}/_apply_op.py" --op finish-plan "$1"
