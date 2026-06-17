#!/usr/bin/env bash
# archive_db.sh — provLedger Phase C-1 cold snapshot.
#
# Copies an existing state-graph DB to provledger.<commit_sha>.db in the same
# directory BEFORE init_project.sh wipes it. This is a COLD ARCHIVE — not in any
# query path — preserving the raw material for future version-over-version
# provenance ("how did this symbol change across versions?") and the ledger's
# fuzzy search ("has a change like this failed before?"). Building the delta
# logic can wait; preserving the snapshots cannot be done retroactively.
#
# Usage: archive_db.sh <db_path>
# No-op (exit 0) if the source DB does not exist.
set -uo pipefail

DB="${1:-}"
if [[ -z "$DB" || ! -f "$DB" ]]; then
    # Nothing to archive (first build, or missing path) — clean no-op.
    exit 0
fi

DIR="$(cd "$(dirname "$DB")" && pwd)"

# Read the recorded commit sha (best-effort); fall back to a timestamp.
SHA=""
if command -v sqlite3 >/dev/null 2>&1; then
    SHA="$(sqlite3 "$DB" \
        "SELECT commit_sha FROM analysis_run ORDER BY id DESC LIMIT 1;" \
        2>/dev/null || echo "")"
fi
if [[ -z "$SHA" || "$SHA" == "NULL" ]]; then
    SHA="unknown-$(date +%Y%m%d%H%M%S)"
fi

ARCHIVE="${DIR}/provledger.${SHA}.db"
cp "$DB" "$ARCHIVE"
echo "    cold archive: ${ARCHIVE}"
exit 0
