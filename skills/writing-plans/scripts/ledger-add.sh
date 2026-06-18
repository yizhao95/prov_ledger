#!/usr/bin/env bash
# ledger-add.sh — thin wrapper around ledger_cli.py (provLedger Phase E).
#
# The decision-memory ledger is MANUAL and OPT-IN: record real decisions and real
# failures by hand. Examples:
#
#   bash scripts/ledger-add.sh add --project sample-project \
#       --kind decision --statement "rolling-window split, not random" \
#       --rationale "random split leaks temporal info" \
#       --subjects train_test_split,split --keywords split,rolling,temporal
#
#   bash scripts/ledger-add.sh list --project sample-project
#
# Environment:
#   ORCH_DB   override the SQLite path (default: ~/skill-workspace/orchestrator.db).

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Resolve a Python interpreter: explicit PYBIN -> repo-local .venv ->
# internal workspace venv -> system python3. Self-contained for a fresh clone.
if [[ -z "${PYBIN:-}" ]]; then
    for _cand in "${SCRIPT_DIR}/../../../.venv/bin/python" "${HOME}/skill-workspace/orchestrator/.venv/bin/python" "$(command -v python3 || true)"; do
        if [[ -n "${_cand}" && -x "${_cand}" ]]; then PYBIN="${_cand}"; break; fi
    done
fi

if [[ ! -x "${PYBIN}" ]]; then
    echo "❌ ledger-add: python interpreter not found at ${PYBIN}" >&2
    exit 3
fi

exec "${PYBIN}" "${SCRIPT_DIR}/ledger_cli.py" "$@"
