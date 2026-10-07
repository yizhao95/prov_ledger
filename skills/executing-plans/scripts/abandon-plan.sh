#!/usr/bin/env bash
# abandon-plan.sh — put down a plan nobody started, with a reason (FL-216).
# Input JSON: {plan_id, reason}. Only an IN_PROGRESS plan whose regular steps never
# left PENDING (or that has none, as a half-done publish left it); the reason lands in
# Deviations. A plan with a started step is refused.
#
# Env: ORCH_DB overrides the SQLite path (default: ~/skill-workspace/orchestrator.db).
set -uo pipefail
if [[ $# -ne 1 ]]; then
    echo "usage: abandon-plan.sh <input.json|yaml|yml>" >&2
    exit 2
fi
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -z "${PYBIN:-}" ]]; then
    for _cand in "${PROVLEDGER_VENV:-${HOME}/skill-workspace/.venv}/bin/python" "${SCRIPT_DIR}/../../../.venv/bin/python" "${HOME}/skill-workspace/orchestrator/.venv/bin/python" "$(command -v python3 || true)"; do
        if [[ -n "${_cand}" && -x "${_cand}" ]]; then PYBIN="${_cand}"; break; fi
    done
fi
exec "${PYBIN}" "${SCRIPT_DIR}/_apply_op.py" --op abandon-plan "$1"
