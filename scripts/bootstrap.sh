#!/usr/bin/env bash
# bootstrap.sh — idempotent dependency self-setup for the provLedger plugin.
# Builds one unified venv and installs requirements.txt. A marker keyed on the
# SHA-256 of requirements.txt makes warm runs a no-op (safe to call on every
# SessionStart).
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
REQS="${PLUGIN_ROOT}/requirements.txt"
VENV="${PROVLEDGER_VENV:-${HOME}/skill-workspace/.venv}"
MARKER="${VENV}/.provledger-reqs.sha256"
LOG="${PROVLEDGER_BOOTSTRAP_LOG:-/tmp/provledger-bootstrap.log}"

if [[ ! -f "${REQS}" ]]; then
    echo "❌ bootstrap: requirements.txt not found at ${REQS}" >&2
    exit 1
fi

want="$(sha256sum "${REQS}" | awk '{print $1}')"

# Warm path: marker matches -> nothing to do.
if [[ -f "${MARKER}" ]] && [[ "$(cat "${MARKER}" 2>/dev/null)" == "${want}" ]]; then
    exit 0
fi

mkdir -p "$(dirname "${VENV}")"

# Test/override hook: a custom installer command replaces venv creation + pip.
if [[ -n "${PROVLEDGER_BOOTSTRAP_INSTALLER:-}" ]]; then
    bash -c "${PROVLEDGER_BOOTSTRAP_INSTALLER}" >>"${LOG}" 2>&1
    rc=$?
else
    {
        if command -v uv >/dev/null 2>&1; then
            uv venv "${VENV}" && \
            VIRTUAL_ENV="${VENV}" uv pip install -r "${REQS}"
        else
            python3 -m venv "${VENV}" && \
            "${VENV}/bin/python" -m pip install --upgrade pip && \
            "${VENV}/bin/python" -m pip install -r "${REQS}"
        fi
    } >>"${LOG}" 2>&1
    rc=$?
fi

if [[ ${rc} -ne 0 ]]; then
    echo "❌ bootstrap: dependency install failed (see ${LOG})" >&2
    exit "${rc}"
fi

echo "${want}" > "${MARKER}"
exit 0
