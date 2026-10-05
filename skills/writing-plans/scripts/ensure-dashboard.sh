#!/usr/bin/env bash
# ensure-dashboard.sh — guarantee the orchestrator dashboard is up at :8765.
#
# The agent never has to read or understand the underlying webapp launcher.
# This script is the single entry point: run it at the start of every plan
# and you're done.
#
# Behavior:
#   1. Curl ${HEALTH_URL} (default: http://127.0.0.1:8765/api/health)
#   2. If up → print "✅ dashboard already up" and exit 0. Up is 200, or 503 with
#      the dashboard's own JSON ({"ok": false, …}): a dashboard started before the
#      first plan created the ledger is running, and launching another collides.
#   3. Otherwise → exec ${LAUNCH_CMD} (default: webapp/launch_dashboard.sh)
#                   then wait up to ${WAIT_SECS} for /api/health to come up
#   4. If still down → exit non-zero with diagnostic
#
# Env overrides (mostly for tests):
#   HEALTH_URL   probe URL   default http://127.0.0.1:8765/api/health
#   LAUNCH_CMD   command to start the dashboard
#                default: bash <plugin root>/orchestrator-webapp/launch_dashboard.sh
#                (plugin root = $CLAUDE_PLUGIN_ROOT, else three dirs above this script)
#   WAIT_SECS    seconds to wait for healthy after launch   default 10
#   ENSURE_DASHBOARD_PRINT_ONLY=1   print the resolved LAUNCH_CMD and exit (tests)

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT:-$(cd "${SCRIPT_DIR}/../../.." && pwd)}"
HEALTH_URL="${HEALTH_URL:-http://127.0.0.1:8765/api/health}"
# FL-012: the bundled webapp is the launcher; the old ~/skill-workspace path only
# existed on the author's machine.
LAUNCH_CMD="${LAUNCH_CMD:-bash ${PLUGIN_ROOT}/orchestrator-webapp/launch_dashboard.sh}"
WAIT_SECS="${WAIT_SECS:-10}"

if [[ "${ENSURE_DASHBOARD_PRINT_ONLY:-0}" == "1" ]]; then
    echo "LAUNCH_CMD=${LAUNCH_CMD}"
    exit 0
fi

probe() {
    # --max-time 2s so we never hang. The status code is the last line of $out.
    local out
    out="$(curl -s --max-time 2 -w '\n%{http_code}' "${HEALTH_URL}" 2>/dev/null)" || return 1
    case "${out##*$'\n'}" in
        200) return 0 ;;
        503) [[ "${out}" == *'"ok": false'* || "${out}" == *'"ok":false'* ]] ;;
        *)   return 1 ;;
    esac
}

if probe; then
    echo "✅ dashboard already up at ${HEALTH_URL}"
    exit 0
fi

echo "▶️  dashboard not responding at ${HEALTH_URL} — launching via: ${LAUNCH_CMD}"
# shellcheck disable=SC2086  # we want word-splitting for LAUNCH_CMD
${LAUNCH_CMD} || true        # don't propagate launcher's exit; we re-probe below

# Wait for healthy
for _ in $(seq 1 "${WAIT_SECS}"); do
    if probe; then
        echo "✅ dashboard up at ${HEALTH_URL}"
        exit 0
    fi
    sleep 1
done

echo "❌ dashboard still unreachable at ${HEALTH_URL} after ${WAIT_SECS}s" >&2
echo "   Tried launcher: ${LAUNCH_CMD}" >&2
echo "   Run manually and inspect the launcher's log: ${PROVLEDGER_DASH_LOG:-/tmp/webapp-server.log}" >&2
exit 1
