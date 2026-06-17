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
PYBIN="${PYBIN:-${HOME}/skill-workspace/orchestrator/.venv/bin/python}"

if [[ ! -x "${PYBIN}" ]]; then
    echo "❌ ledger-add: python interpreter not found at ${PYBIN}" >&2
    exit 3
fi

exec "${PYBIN}" "${SCRIPT_DIR}/ledger_cli.py" "$@"
