---
description: Launch the read-only provLedger review dashboard (bundled webapp) on localhost.
---

Launch the provLedger dashboard using the plugin's bundled webapp and the unified venv.

Run this command:

```bash
PROVLEDGER_WEBAPP_DIR="${CLAUDE_PLUGIN_ROOT}/orchestrator-webapp" \
  bash "${CLAUDE_PLUGIN_ROOT}/orchestrator-webapp/launch_dashboard.sh"
```

Then report the dashboard URL to the user. The dashboard reads the orchestrator
SQLite DB **read-only** (path from `ORCH_DB`, default `~/skill-workspace/orchestrator.db`).
Override the port with `PROVLEDGER_DASH_PORT` if 8765 is taken.
