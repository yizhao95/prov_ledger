#!/usr/bin/env bash
# launch_dashboard.sh — idempotent launcher for the provLedger dashboard.
#
# Usage: bash launch_dashboard.sh
#
# Behavior:
#   - If port 8765 is already serving the dashboard, do nothing (exit 0).
#   - Else: start uvicorn in the background, log to /tmp/webapp-server.log, print URL.
#   - Before the first plan there is no ledger and /api/health answers 503 with
#     {"ok": false, ...}. That is the dashboard running, not failing: it shows the
#     empty state and picks the ledger up when the first plan is recorded. Both
#     runs report "running, no ledger yet" and exit 0.
#
# Env:
#   ORCH_DB  overrides the SQLite path (default: ~/skill-workspace/orchestrator.db).
#   PROVLEDGER_DASH_WAIT  seconds to wait for the server to answer (default 5).
#
# Designed to be called from the writing-plans skill at the top of every plan.

set -uo pipefail

PORT="${PROVLEDGER_DASH_PORT:-8765}"
URL="http://127.0.0.1:${PORT}"
# App dir: prefer the bundled webapp (this script's own dir), overridable for
# the legacy workspace copy via PROVLEDGER_WEBAPP_DIR.
APP_DIR="${PROVLEDGER_WEBAPP_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
VENV="${PROVLEDGER_VENV:-${HOME}/skill-workspace/.venv}"
VENV_PY="${VENV}/bin/uvicorn"
LOG="${PROVLEDGER_DASH_LOG:-/tmp/webapp-server.log}"
WAIT="${PROVLEDGER_DASH_WAIT:-5}"
LEDGER="${ORCH_DB:-${HOME}/skill-workspace/orchestrator.db}"

# probe → prints one word: "ok" (200), "no-ledger" (503, our dashboard, no DB
# yet), "db-error" (503, our dashboard, the DB is locked or unreadable), or
# "down" (no answer, or an answer that is not this dashboard's health JSON).
probe() {
    local body code
    body="$(curl -s --max-time 2 -w '\n%{http_code}' "${URL}/api/health" 2>/dev/null)" || { echo down; return; }
    code="${body##*$'\n'}"
    body="${body%$'\n'*}"
    if [[ "${code}" == "200" ]]; then
        echo ok
    elif [[ "${code}" == "503" && "${body}" == *'"ok":false'* ]]; then
        if [[ "${body}" == *"orchestrator.db not found"* ]]; then echo no-ledger; else echo db-error; fi
    else
        echo down
    fi
}

# report <state> <prefix> → prints the line for a running dashboard
report() {
    case "$1" in
        ok)        echo "✅ $2 at ${URL}" ;;
        no-ledger) echo "✅ $2 at ${URL}, no ledger yet (${LEDGER}); it appears when the first plan is recorded" ;;
        db-error)  echo "⚠️  $2 at ${URL}, but the ledger is unreadable right now (${LEDGER}); see ${URL}/api/health" ;;
    esac
}

# 1. Already running? (with or without a ledger)
STATE="$(probe)"
if [[ "${STATE}" != "down" ]]; then
    report "${STATE}" "Dashboard already running"
    exit 0
fi

# 2. Port occupied but not by us? Bail loudly — don't kill what we don't own.
if lsof -ti:${PORT} > /dev/null 2>&1; then
    echo "❌ Port ${PORT} is in use but health check failed."
    echo "   Investigate: lsof -ti:${PORT} → $(lsof -ti:${PORT} | tr '\n' ' ')"
    exit 1
fi

# 3. Sanity: app dir exists?
if [[ ! -x "${VENV_PY}" ]]; then
    echo "❌ uvicorn not found at ${VENV_PY}"
    echo "   Run: bash \"${CLAUDE_PLUGIN_ROOT:-<plugin-root>}/scripts/bootstrap.sh\" to install dependencies."
    exit 1
fi

# 4. Launch in background, logged
cd "${APP_DIR}"
nohup "${VENV_PY}" app.main:app --host 127.0.0.1 --port ${PORT} > "${LOG}" 2>&1 &
PID=$!
echo "🚀 Launching dashboard (PID ${PID}, log → ${LOG})"

# 5. Wait up to ${WAIT}s for the server to answer
for _ in $(seq 1 "${WAIT}"); do
    sleep 1
    STATE="$(probe)"
    if [[ "${STATE}" != "down" ]]; then
        report "${STATE}" "Dashboard ready"
        exit 0
    fi
done

echo "⚠️  Dashboard PID ${PID} did not answer within ${WAIT}s — check ${LOG}"
exit 1
