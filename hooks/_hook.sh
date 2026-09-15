#!/usr/bin/env bash
# _hook.sh <event> — shared body of the provLedger Claude Code hooks (toolcall.sh,
# utterance.sh). Runs `python -m orchestrator.hooks <event>` with the hook's JSON
# on stdin. Contract: stdout is ALWAYS empty (a UserPromptSubmit hook's stdout is
# injected as context), exit is ALWAYS 0; problems go to the error log.
set -uo pipefail
EVENT="${1:-}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT:-$(cd "${SCRIPT_DIR}/.." && pwd)}"
ERRLOG="${PROVLEDGER_HOOK_ERRORS:-${HOME}/skill-workspace/hook-errors.log}"
mkdir -p "$(dirname "${ERRLOG}")" 2>/dev/null || true
# Same interpreter resolution as skills/executing-plans/scripts/reason-fill.sh.
if [[ -z "${PYBIN:-}" ]]; then
    for _cand in "${PROVLEDGER_VENV:-${HOME}/skill-workspace/.venv}/bin/python" "${PLUGIN_ROOT}/.venv/bin/python" "${HOME}/skill-workspace/orchestrator/.venv/bin/python" "$(command -v python3 || true)"; do
        if [[ -n "${_cand}" && -x "${_cand}" ]]; then PYBIN="${_cand}"; break; fi
    done
fi
if [[ -z "${PYBIN:-}" ]]; then
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) ${EVENT:--} NoInterpreter: no python found for the hook" >> "${ERRLOG}" 2>/dev/null
    exit 0
fi
PYTHONPATH="${PLUGIN_ROOT}/orchestrator-backend${PYTHONPATH:+:${PYTHONPATH}}" \
    "${PYBIN}" -m orchestrator.hooks "${EVENT}" >/dev/null 2>>"${ERRLOG}" || true
exit 0
