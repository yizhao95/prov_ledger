#!/usr/bin/env bash
# agent-review-close.sh — deterministic finalize for a NEEDS_REVIEW plan.
#
# Used ONLY by the update-project-state-graph review sub-agent to close a plan
# that the orchestrator parked in NEEDS_REVIEW (registered-project completion).
# Input JSON: {plan_id, outcome: "pass"|"fail", summary?, log_context?}
#   pass -> review step NEEDS_REVIEW -> COMPLETED, plan COMPLETED
#   fail -> review step NEEDS_REVIEW -> FAILED,    plan FAILED (details logged)
# See ~/.code_puppy/skills/executing-plans/update-input.example.json.
#
# Env: ORCH_DB overrides the SQLite path (default: ~/skill-workspace/orchestrator.db).
set -uo pipefail
if [[ $# -ne 1 ]]; then
    echo "usage: agent-review-close.sh <input.json|yaml|yml>" >&2
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
exec "${PYBIN}" "${SCRIPT_DIR}/_apply_op.py" --op agent-review-close "$1"
