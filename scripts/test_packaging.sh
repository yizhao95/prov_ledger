#!/usr/bin/env bash
# Packaging/install test case for the pip-installable `provledger` library.
#
# Builds the wheel + sdist from orchestrator-backend/, creates a FRESH venv,
# installs each artifact, and runs scripts/pkg_smoke_test.py from OUTSIDE the
# repo (so only the installed package is importable). Fails loudly on any gap
# (e.g. migrations missing from the wheel).
#
# Needs `uv` on PATH (used for both build and venv — no python3-venv needed).
# Usage, from the repo root:  bash scripts/test_packaging.sh
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND="${REPO}/orchestrator-backend"
SMOKE="${REPO}/scripts/pkg_smoke_test.py"
WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT

command -v uv >/dev/null || { echo "❌ needs uv (https://docs.astral.sh/uv/)"; exit 1; }

echo "── build ──"
rm -rf "${BACKEND}/dist"
(cd "${BACKEND}" && uv build --quiet)
ls "${BACKEND}"/dist/

for artifact in "${BACKEND}"/dist/provledger-*.whl "${BACKEND}"/dist/provledger-*.tar.gz; do
    echo "── install test: $(basename "${artifact}") ──"
    VENV="${WORK}/venv-$(basename "${artifact}" | tr '.' '-')"
    uv venv --quiet "${VENV}"
    uv pip install --quiet --python "${VENV}/bin/python" "${artifact}"
    # run from a neutral cwd so the repo source can't shadow the install
    (cd "${WORK}" && "${VENV}/bin/python" "${SMOKE}")
done

echo "✅ packaging test: wheel + sdist both install and pass the smoke test"
