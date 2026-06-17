#!/usr/bin/env bash
# Test for archive_db.sh (provLedger Phase C-1 cold snapshot).
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ARCHIVE="${HERE}/../archive_db.sh"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
fail=0
note() { echo "  - $1"; }

# ── fixture: a DB with a recorded commit_sha ─────────────────────────────────────
DB="${TMP}/proj-state-graph.db"
sqlite3 "$DB" "CREATE TABLE analysis_run (id INTEGER PRIMARY KEY, commit_sha TEXT);
               INSERT INTO analysis_run (commit_sha) VALUES ('abc1234def');
               CREATE TABLE node (id INTEGER); INSERT INTO node VALUES (1);"

# ── 1. archives to provledger.<sha>.db, byte-identical ───────────────────────────
bash "$ARCHIVE" "$DB" >/dev/null 2>&1
EXPECT="${TMP}/provledger.abc1234def.db"
if [[ -f "$EXPECT" ]]; then note "archive created: PASS"; else note "archive created: FAIL"; fail=1; fi
if cmp -s "$DB" "$EXPECT"; then note "byte-identical: PASS"; else note "byte-identical: FAIL"; fail=1; fi

# ── 2. missing source DB -> no-op, exit 0, no file ───────────────────────────────
MISSING="${TMP}/nope.db"
if bash "$ARCHIVE" "$MISSING" >/dev/null 2>&1; then
  note "missing-source exit 0: PASS"
else
  note "missing-source exit 0: FAIL"; fail=1
fi
if [[ ! -e "${TMP}/provledger.unknown.db" && ! -e "${TMP}/nope"* ]]; then
  note "missing-source no file: PASS"
else
  note "missing-source no file: FAIL"; fail=1
fi

if [[ "$fail" -eq 0 ]]; then echo "archive_db.sh tests: ALL PASS"; else echo "archive_db.sh tests: FAILURES"; fi
exit "$fail"
