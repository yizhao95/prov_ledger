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
# Resolve a Python interpreter: explicit PYBIN -> repo-local .venv ->
# internal workspace venv -> system python3. Self-contained for a fresh clone.
if [[ -z "${PYBIN:-}" ]]; then
    for _cand in "${PROVLEDGER_VENV:-${HOME}/skill-workspace/.venv}/bin/python" "${SCRIPT_DIR}/../../../.venv/bin/python" "${HOME}/skill-workspace/orchestrator/.venv/bin/python" "$(command -v python3 || true)"; do
        if [[ -n "${_cand}" && -x "${_cand}" ]]; then PYBIN="${_cand}"; break; fi
    done
fi
exec "${PYBIN}" "${SCRIPT_DIR}/_apply_op.py" --op finish-plan "$1"
