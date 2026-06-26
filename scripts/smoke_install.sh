#!/usr/bin/env bash
# smoke_install.sh — exercise a cold bootstrap into a throwaway venv.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "${TMP}"' EXIT
echo "Cold bootstrap into ${TMP}/venv ..."
PROVLEDGER_VENV="${TMP}/venv" bash "${PLUGIN_ROOT}/scripts/bootstrap.sh"
test -x "${TMP}/venv/bin/python" || { echo "❌ venv python missing"; exit 1; }
"${TMP}/venv/bin/python" -c "import fastapi, jinja2, tree_sitter; print('✅ deps import OK')"
echo "Warm re-run (must be a no-op) ..."
PROVLEDGER_VENV="${TMP}/venv" bash "${PLUGIN_ROOT}/scripts/bootstrap.sh"
echo "✅ smoke install passed"
