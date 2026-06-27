#!/usr/bin/env bash
# launch_dashboard.sh — idempotent launcher for the provLedger dashboard.
#
# Usage: bash launch_dashboard.sh
#
# Behavior:
#   - If port 8765 is already serving the dashboard (health check passes), do nothing.
#   - Else: start uvicorn in the background, log to /tmp/webapp-server.log, print URL.
#
# Env:
#   ORCH_DB  overrides the SQLite path (default: ~/skill-workspace/orchestrator.db).
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

# 1. Already running and healthy?
if curl -sf "${URL}/api/health" > /dev/null 2>&1; then
    echo "✅ Dashboard already running at ${URL}"
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

# 5. Wait up to 5s for health
for i in 1 2 3 4 5; do
    sleep 1
    if curl -sf "${URL}/api/health" > /dev/null 2>&1; then
        echo "✅ Dashboard ready at ${URL}"
        exit 0
    fi
done

echo "⚠️  Dashboard PID ${PID} did not pass health check within 5s — check ${LOG}"
exit 1
