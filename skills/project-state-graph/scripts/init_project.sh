#!/usr/bin/env bash
# init_project.sh — one-command project state-graph initializer.
#
# Builds BOTH layers for a repo and registers it:
#   1. deep layer : <out-dir>/<name>-state-graph.db   (via analyzer)
#   2. shallow    : <out-dir>/ARCHITECTURE.md         (via architecture_md.py)
#   3. registry   : projects.json + PROJECT-STATE-GRAPHS.md index
#   4. verify     : selfcheck invariants on the built DB
#
# Usage:
#   init_project.sh --name NAME --repo REPO_PATH [--out-dir DIR]
#                   [--trigger session --session-id SID --notify-orch-db ORCH_DB]   (Stop hook, DP phase 2)
#
# Defaults:
#   --out-dir  ~/skill-workspace/project-graphs/<name>
#
# Deterministic: no prompts, exits non-zero on any failure.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

NAME=""
REPO=""
OUT_DIR=""
SESSION_ID=""
NOTIFY_ORCH_DB=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --name)    NAME="$2";    shift 2 ;;
        --repo)    REPO="$2";    shift 2 ;;
        --out-dir) OUT_DIR="$2"; shift 2 ;;
        # DP phase 2 (Task 7b): a session refresh — trigger, the session it belongs to,
        # and the orchestrator DB to report done|failed back to (session_run).
        --trigger)        PROVLEDGER_TRIGGER="$2"; shift 2 ;;
        --session-id)     SESSION_ID="$2";         shift 2 ;;
        --notify-orch-db) NOTIFY_ORCH_DB="$2";     shift 2 ;;
        -h|--help)
            grep '^#' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
            exit 0 ;;
        *)
            echo "ERROR: unknown argument: $1" >&2
            exit 2 ;;
    esac
done

if [[ -z "$NAME" || -z "$REPO" ]]; then
    echo "ERROR: --name and --repo are required" >&2
    echo "usage: init_project.sh --name NAME --repo REPO_PATH [--out-dir DIR]" >&2
    exit 2
fi

if [[ ! -d "$REPO" ]]; then
    echo "ERROR: repo path does not exist: $REPO" >&2
    exit 2
fi

# Resolve absolute repo path.
REPO="$(cd "$REPO" && pwd)"

# Default out dir.
if [[ -z "$OUT_DIR" ]]; then
    OUT_DIR="${PSG_REGISTRY_ROOT:-${HOME}/skill-workspace/project-graphs}/${NAME}"
fi

DB_PATH="${OUT_DIR}/${NAME}-state-graph.db"
ARCH_PATH="${OUT_DIR}/ARCHITECTURE.md"

# DP phase 2 (Task 7b): report back to the orchestrator DB when asked — done on a clean
# exit, failed otherwise — and drop the refresh lock the Stop hook wrote. Never fatal.
_notify() {
    local rc=$?
    if [[ -n "$SESSION_ID" && -n "$NOTIFY_ORCH_DB" ]]; then
        local state="failed"; [[ $rc -eq 0 ]] && state="done"
        local pybin="${PROVLEDGER_PYBIN:-}"
        if [[ -z "$pybin" ]]; then
            for _cand in "${PROVLEDGER_VENV:-${HOME}/skill-workspace/.venv}/bin/python" "${SCRIPT_DIR}/../../../.venv/bin/python" "$(command -v python3 || true)"; do
                if [[ -n "${_cand}" && -x "${_cand}" ]]; then pybin="${_cand}"; break; fi
            done
        fi
        if [[ -n "$pybin" ]]; then
            PYTHONPATH="${SCRIPT_DIR}/../../../orchestrator-backend${PYTHONPATH:+:${PYTHONPATH}}" \
                "$pybin" -m orchestrator.session refreshed --session-id "$SESSION_ID" --state "$state" \
                    --orch-db "$NOTIFY_ORCH_DB" --psg-db "$DB_PATH" >/dev/null 2>&1 \
                || echo "    WARNING: could not notify ${NOTIFY_ORCH_DB} (session ${SESSION_ID}, ${state})" >&2
        fi
        rm -f "${OUT_DIR}/.refresh.lock" 2>/dev/null || true
    fi
}
trap _notify EXIT
# Registry + index locations (env-overridable for isolated testing).
PSG_REGISTRY_ROOT="${PSG_REGISTRY_ROOT:-${HOME}/skill-workspace/project-graphs}"
REGISTRY_PATH="${PSG_REGISTRY_PATH:-${PSG_REGISTRY_ROOT}/projects.json}"
INDEX_PATH="${PSG_INDEX_PATH:-${PSG_REGISTRY_ROOT}/PROJECT-STATE-GRAPHS.md}"

echo "==> project-state-graph init: ${NAME}"
echo "    repo:    ${REPO}"
echo "    out-dir: ${OUT_DIR}"

# 1. Ensure out dir.
mkdir -p "$OUT_DIR"

# Run everything inside the skill's uv env so deps resolve.
cd "$SCRIPT_DIR"

# 2. Deep layer — build the state-graph DB.
# store.reset_graph (PSG-C1) makes the rebuild idempotent; the history tables
# (node_snapshot / node_event) must survive — NEVER delete the DB file here.
# (Phase-1 dogfood: an `rm -f "$DB_PATH"` used to wipe the history on every refresh.)
echo "==> [1/5] building deep layer (analyzer) -> ${DB_PATH}"
# Phase C-1: cold-snapshot the prior graph before it is rebuilt (non-fatal).
# A clean archive at provledger.<old_sha>.db preserves raw material for future
# version-over-version provenance; it cannot be captured retroactively.
if [[ -f "$DB_PATH" ]]; then
    bash "${SCRIPT_DIR}/archive_db.sh" "$DB_PATH" \
        || echo "    WARNING: cold archive failed (non-fatal)" >&2
fi
# Run attribution (spec §2.5): the orchestrator step that caused this refresh,
# passed through the environment by update-project-state-graph.
uv run python -m analyzer "$REPO" --project "$NAME" --db-path "$DB_PATH" \
    ${PROVLEDGER_PLAN_ID:+--plan-id "$PROVLEDGER_PLAN_ID"} \
    ${PROVLEDGER_STEP_ID:+--step-id "$PROVLEDGER_STEP_ID"} \
    --trigger "${PROVLEDGER_TRIGGER:-manual}" \
    ${SESSION_ID:+--session-id "$SESSION_ID"} \
    ${PROVLEDGER_ISOLATE:+--isolate "$PROVLEDGER_ISOLATE"}

# Capture the commit sha that was analyzed (best-effort).
COMMIT_SHA="$(git -C "$REPO" rev-parse HEAD 2>/dev/null || echo "")"

# 3. Shallow layer — ARCHITECTURE.md.
echo "==> [2/5] building shallow layer (ARCHITECTURE.md) -> ${ARCH_PATH}"
uv run python architecture_md.py "$DB_PATH" "$NAME" "$ARCH_PATH"

# 4. Registry + index.
echo "==> [3/5] updating registry -> ${REGISTRY_PATH}"
uv run python - "$REGISTRY_PATH" "$INDEX_PATH" "$NAME" "$REPO" "$DB_PATH" "$COMMIT_SHA" <<'PY'
import sys
import registry

reg_path, index_path, name, repo, db_path, commit_sha = sys.argv[1:7]
registry.add_project(reg_path, name, repo, db_path, commit_sha or None)
registry.regenerate_index(reg_path, index_path)
print(f"    registered {name} -> {db_path}")
PY

# 5. Verify — selfcheck invariants.
echo "==> [4/5] verifying built graph (selfcheck)"
uv run python selfcheck.py "$DB_PATH"

# 6. DataFrame-aware slices (provLedger Phase B) — additive, non-fatal.
SLICES_PATH="${OUT_DIR}/${NAME}-slices.html"
echo "==> [5/5] rendering DataFrame-aware slices -> ${SLICES_PATH}"
if uv run python viz_slices.py "$DB_PATH" "$SLICES_PATH" --title "$NAME"; then
    :
else
    echo "    WARNING: slice render failed (non-fatal); graph build is unaffected" >&2
fi

echo "==> done: ${NAME}"
echo "    deep:    ${DB_PATH}"
echo "    shallow: ${ARCH_PATH}"
echo "    slices:  ${SLICES_PATH}"
echo "    index:   ${INDEX_PATH}"
