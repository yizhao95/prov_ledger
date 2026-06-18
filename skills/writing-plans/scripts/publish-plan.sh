#!/usr/bin/env bash
# publish-plan.sh — write a plan-input file (JSON or YAML) to the orchestrator SQLite DB.
#
# This is the ONLY way the writing-plans skill should create plans. The agent
# composes a plan-input.{json,yaml} file and runs:
#
#     bash scripts/publish-plan.sh /path/to/plan-input.json
#
# The script delegates to publish_plan.py (Python helper) which:
#   - parses + validates the input file (clear errors with field names)
#   - calls orchestrator.api.initialize_plan() — the canonical write path
#   - prints the JSON result {plan_id, step_ids, skills_recorded} to stdout
#
# Environment:
#   ORCH_DB   override the SQLite path (default: ~/skill-workspace/orchestrator.db).
#             Tests use this to redirect to an ephemeral DB.
#
# Exit:  0 on success, non-zero on validation or IO failure.

set -uo pipefail

if [[ $# -ne 1 ]]; then
    echo "usage: publish-plan.sh <plan-input.json|yaml|yml>" >&2
    exit 2
fi

INPUT="$1"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Resolve a Python interpreter: explicit PYBIN -> repo-local .venv ->
# internal workspace venv -> system python3. Self-contained for a fresh clone.
if [[ -z "${PYBIN:-}" ]]; then
    for _cand in "${SCRIPT_DIR}/../../../.venv/bin/python" "${HOME}/skill-workspace/orchestrator/.venv/bin/python" "$(command -v python3 || true)"; do
        if [[ -n "${_cand}" && -x "${_cand}" ]]; then PYBIN="${_cand}"; break; fi
    done
fi

if [[ ! -x "${PYBIN}" ]]; then
    echo "❌ publish-plan: python interpreter not found at ${PYBIN}" >&2
    echo "   Set PYBIN=/path/to/python or fix the orchestrator venv." >&2
    exit 3
fi

exec "${PYBIN}" "${SCRIPT_DIR}/publish_plan.py" "${INPUT}"
