#!/usr/bin/env bash
# ensure-dashboard.sh — guarantee the orchestrator dashboard is up at :8765.
#
# The agent never has to read or understand the underlying webapp launcher.
# This script is the single entry point: run it at the start of every plan
# and you're done.
#
# Behavior:
#   1. Curl ${HEALTH_URL} (default: http://127.0.0.1:8765/api/health)
#   2. If 200 → print "✅ dashboard already up" and exit 0
#   3. Otherwise → exec ${LAUNCH_CMD} (default: webapp/launch_dashboard.sh)
#                   then wait up to ${WAIT_SECS} for /api/health to come up
#   4. If still down → exit non-zero with diagnostic
#
# Env overrides (mostly for tests):
#   HEALTH_URL   probe URL   default http://127.0.0.1:8765/api/health
#   LAUNCH_CMD   command to start the dashboard
#                default: bash ~/skill-workspace/orchestrator-webapp/launch_dashboard.sh
#   WAIT_SECS    seconds to wait for healthy after launch   default 10

set -uo pipefail

HEALTH_URL="${HEALTH_URL:-http://127.0.0.1:8765/api/health}"
LAUNCH_CMD="${LAUNCH_CMD:-bash ${HOME}/skill-workspace/orchestrator-webapp/launch_dashboard.sh}"
WAIT_SECS="${WAIT_SECS:-10}"

probe() {
    # 200 + non-empty body counts as healthy. -s silent, -f fail-fast on 4xx/5xx,
    # --max-time 2s so we never hang.
    curl -sf --max-time 2 "${HEALTH_URL}" >/dev/null 2>&1
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
echo "   Run manually and inspect logs at /tmp/dashboard.log" >&2
exit 1
