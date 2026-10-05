#!/usr/bin/env bash
# bootstrap.sh — idempotent dependency self-setup for the provLedger plugin.
# Builds one unified venv and installs requirements.txt. A marker keyed on the
# SHA-256 of everything that install reads -- requirements.txt and the editable
# backend's orchestrator-backend/pyproject.toml (its version, its console
# script) -- makes warm runs a no-op (safe to call on every SessionStart).
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
REQS="${PLUGIN_ROOT}/requirements.txt"
BACKEND_PYPROJECT="${PLUGIN_ROOT}/orchestrator-backend/pyproject.toml"
VENV="${PROVLEDGER_VENV:-${HOME}/skill-workspace/.venv}"
[[ "${VENV}" == /* ]] || VENV="${PWD}/${VENV}"   # absolute: the install below runs from the plugin root
MARKER="${VENV}/.provledger-reqs.sha256"
LOG="${PROVLEDGER_BOOTSTRAP_LOG:-/tmp/provledger-bootstrap.log}"

if [[ ! -f "${REQS}" ]]; then
    echo "❌ bootstrap: requirements.txt not found at ${REQS}" >&2
    exit 1
fi

# requirements.txt installs the backend editable (-e ./orchestrator-backend), so
# a version bump in its pyproject.toml must re-install even when requirements.txt
# is unchanged: hashing requirements.txt alone left venvs on stale metadata and
# without the `provledger` console script. Content only, never paths.
want="$(cat "${REQS}" "${BACKEND_PYPROJECT}" 2>/dev/null | sha256sum | awk '{print $1}')"

# Same-named skill notice: provledger ships local variants of six superpowers
# skills (writing-plans, executing-plans, ...). When both plugins are enabled
# user-wide, tell the user how to let provLedger's variants win in this project
# — unless the project's .claude/settings.local.json already disables
# superpowers (i.e. the user followed the advice). Settings are parsed as JSON.
_same_named_skill_notice() {
    if python3 - "${HOME}/.claude/settings.json" "${PWD}/.claude/settings.local.json" <<'PY'
import json, sys

def enabled_plugins(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f).get("enabledPlugins") or {}
    except (OSError, ValueError, AttributeError):
        return {}

user, project = (enabled_plugins(p) for p in sys.argv[1:3])
SP, PL = "superpowers@claude-plugins-official", "provledger@provledger"
both = user.get(SP) is True and user.get(PL) is True
sys.exit(0 if both and project.get(SP) is not False else 1)
PY
    then
        echo "ℹ️  provLedger: superpowers is also enabled — both provide same-named skills" \
             "(writing-plans, executing-plans, …). To let provLedger's variants win in this project:" \
             "  claude plugin disable superpowers@claude-plugins-official --scope local"
    fi
}

# Warm path: marker matches -> nothing to do.
if [[ -f "${MARKER}" ]] && [[ "$(cat "${MARKER}" 2>/dev/null)" == "${want}" ]]; then
    echo "✅ provLedger bootstrap: dependencies up to date (warm no-op)"
    _same_named_skill_notice
    exit 0
fi

mkdir -p "$(dirname "${VENV}")"

# Test/override hook: a custom installer command replaces venv creation + pip.
if [[ -n "${PROVLEDGER_BOOTSTRAP_INSTALLER:-}" ]]; then
    bash -c "${PROVLEDGER_BOOTSTRAP_INSTALLER}" >>"${LOG}" 2>&1
    rc=$?
else
    # From the plugin root, in a subshell: uv and pip resolve the relative
    # `-e ./orchestrator-backend` in requirements.txt against the cwd, and
    # SessionStart runs this from the user's project. The notice below still
    # needs the project's ${PWD}, so only the install moves.
    (
        cd "${PLUGIN_ROOT}" || exit 1
        if command -v uv >/dev/null 2>&1; then
            # uv >= 0.5 refuses to overwrite an existing venv (--clear would
            # wipe it) — reuse it and just sync the requirements instead.
            { [[ -x "${VENV}/bin/python" ]] || uv venv "${VENV}"; } && \
            VIRTUAL_ENV="${VENV}" uv pip install -r "${REQS}"
        else
            python3 -m venv "${VENV}" && \
            "${VENV}/bin/python" -m pip install --upgrade pip && \
            "${VENV}/bin/python" -m pip install -r "${REQS}"
        fi
    ) >>"${LOG}" 2>&1
    rc=$?
fi

if [[ ${rc} -ne 0 ]]; then
    echo "❌ bootstrap: dependency install failed (see ${LOG})" >&2
    exit "${rc}"
fi

echo "${want}" > "${MARKER}"
echo "✅ provLedger bootstrap: venv ready at ${VENV}"
_same_named_skill_notice
exit 0
