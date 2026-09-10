#!/usr/bin/env bash
# Test for archive_db.sh (provLedger Phase C-1 cold snapshot).
# No sqlite3 CLI is required — neither here nor by the script under test
# (phase-1 dogfood: without the CLI the archive name degraded to unknown-<ts>).
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ARCHIVE="${HERE}/../archive_db.sh"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
fail=0
note() { echo "  - $1"; }

# Hide any sqlite3 CLI: the script must read the sha with python3 alone.
SHIM="${TMP}/bin"; mkdir -p "$SHIM"
printf '#!/usr/bin/env bash\necho "sqlite3 CLI must not be used" >&2; exit 127\n' > "${SHIM}/sqlite3"; chmod +x "${SHIM}/sqlite3"
export PATH="${SHIM}:${PATH}"

# ── fixture: a DB with a recorded commit_sha ─────────────────────────────────────
DB="${TMP}/proj-state-graph.db"
python3 - "$DB" <<'PY'
import sqlite3, sys
c = sqlite3.connect(sys.argv[1])
c.executescript("CREATE TABLE analysis_run (id INTEGER PRIMARY KEY, commit_sha TEXT);"
                "INSERT INTO analysis_run (commit_sha) VALUES ('abc1234def');"
                "CREATE TABLE node (id INTEGER); INSERT INTO node VALUES (1);")
c.commit(); c.close()
PY

# ── 1. archives to provledger.<sha>.db, byte-identical ───────────────────────────
bash "$ARCHIVE" "$DB" >/dev/null 2>&1
EXPECT="${TMP}/provledger.abc1234def.db"
if [[ -f "$EXPECT" ]]; then note "archive named by real sha: PASS"; else note "archive named by real sha: FAIL ($(ls "$TMP"))"; fail=1; fi
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

# ── 3. no recorded sha -> unknown-<timestamp> (visible degradation, not a crash) ──
DB2="${TMP}/nosha-state-graph.db"
python3 -c "import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); c.execute('CREATE TABLE node (id INTEGER)'); c.commit()" "$DB2"
bash "$ARCHIVE" "$DB2" >/dev/null 2>&1
if ls "${TMP}"/provledger.unknown-*.db >/dev/null 2>&1; then note "no-sha falls back to unknown-<ts>: PASS"; else note "no-sha falls back to unknown-<ts>: FAIL"; fail=1; fi

if [[ "$fail" -eq 0 ]]; then echo "archive_db.sh tests: ALL PASS"; else echo "archive_db.sh tests: FAILURES"; fi
exit "$fail"
