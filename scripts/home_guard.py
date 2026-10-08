#!/usr/bin/env python3
"""home_guard.py — did a test suite write to the real ~/skill-workspace?

    home_guard.py snapshot <file>   record the workspace's state
    home_guard.py check <file>      exit 1, naming each change, if a suite leaked

run_tests.sh takes a snapshot before every suite and checks it after.

What counts as a leak is chosen so that the provLedger hooks of the session the
suites are run from never trip it: those hooks append tool calls and prompts to
the real ledger, and the Stop hook rewrites the project's graph and its index,
all while the suites run. So the guard compares only what a leaking test changes
and a hook does not:

  * the set of projects in project-graphs/projects.json and in the
    PROJECT-STATE-GRAPHS.md index (a test registry regenerates the index and
    drops the real projects from it);
  * the newest row of the ledger tables only plans, declarations and questions
    write (a plan published by hand while the suites run will be reported too).
    The one hook write among them is the Stop hook's degraded-mode close, which
    files reasons under a `session:<id>` plan; those rows are left out;
  * the ledger's applied migrations (`schema_version`) and its journal mode. A
    test that opens the real ledger with the working tree's code runs the
    branch's migrations on it and switches it to WAL, without adding a row
    anywhere (FL-237). The hooks run the installed release, whose migrations
    are already applied and which already put the ledger in WAL;
  * the entries at the top of the workspace, and whether it exists at all —
    leaving out SQLite's sidecar files (-journal, -wal, -shm), which exist for
    the length of one write and which the hooks create all the time.

The workspace is $PROVLEDGER_GUARD_HOME, else ~/skill-workspace. Stdlib only.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

LEDGER_TABLES = ("Plans", "Steps", "Deviations", "LedgerEntries", "change_reason", "reference",
                 "declared_node", "expectations", "outcomes", "ask_log")
# Rows a hook writes into a guarded table, by table: the Stop hook closes a
# session that published no plan with reasons filed under `session:<id>`.
SQLITE_SIDECARS = ("-journal", "-wal", "-shm")
# The hooks' spool (FL-193): orchestrator.db.spool.jsonl, and .<pid> while one replays it.
SPOOL_MARK = ".spool.jsonl"
HOOK_ROWS = {"change_reason": "coalesce(plan_id, '') LIKE 'session:%'"}


def _home() -> Path:
    return Path(os.environ.get("PROVLEDGER_GUARD_HOME") or Path.home() / "skill-workspace")


def _registry(graphs: Path) -> list[str]:
    try:
        data = json.loads((graphs / "projects.json").read_text())
    except (OSError, ValueError):
        return []
    return sorted(p.get("name", "") for p in data.get("projects", []))


def _indexed(graphs: Path) -> list[str]:
    """Project names in the first column of the index table."""
    try:
        lines = (graphs / "PROJECT-STATE-GRAPHS.md").read_text().splitlines()
    except OSError:
        return []
    names = []
    for line in lines:
        if not line.startswith("|"):
            continue
        cell = line.split("|")[1].strip()
        if cell and cell != "Project" and not set(cell) <= set("-: "):
            names.append(cell)
    return sorted(names)


def _ledger(db: Path) -> dict[str, int | None]:
    if not db.exists():
        return {}
    out: dict[str, int | None] = {}
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
    try:
        for table in LEDGER_TABLES:
            where = f" WHERE NOT ({HOOK_ROWS[table]})" if table in HOOK_ROWS else ""
            try:
                out[table] = conn.execute(f'SELECT max(rowid) FROM "{table}"{where}').fetchone()[0]
            except sqlite3.Error:
                continue                        # table (or column) absent in this ledger
    finally:
        conn.close()
    return out


def _schema(db: Path) -> dict:
    """The migration files applied to the ledger, and its journal mode. A row
    from before schema_version had a migration_file column has none; every
    migration applied since records its file name."""
    if not db.exists():
        return {}
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
    try:
        try:
            migrations = sorted(r[0] for r in conn.execute(
                "SELECT migration_file FROM schema_version WHERE migration_file IS NOT NULL"))
        except sqlite3.Error:
            migrations = []                     # a ledger no migration has run on
        return {"migrations": migrations, "journal_mode": conn.execute("PRAGMA journal_mode").fetchone()[0]}
    finally:
        conn.close()


def snapshot() -> dict:
    home = _home()
    if not home.exists():
        return {"exists": False}
    graphs = home / "project-graphs"
    return {
        "exists": True,
        "entries": sorted(p.name for p in home.iterdir() if not p.name.endswith(SQLITE_SIDECARS) and SPOOL_MARK not in p.name),
        "registry": _registry(graphs),
        "index": _indexed(graphs),
        "ledger": _ledger(home / "orchestrator.db"),
        "schema": _schema(home / "orchestrator.db"),
    }


def diff(before: dict, after: dict) -> list[str]:
    home = _home()
    if not before["exists"]:
        return [f"{home} was created"] if after["exists"] else []
    if not after["exists"]:
        return [f"{home} was deleted"]
    changes = []
    added = sorted(set(after["entries"]) - set(before["entries"]))
    removed = sorted(set(before["entries"]) - set(after["entries"]))
    if added:
        changes.append(f"new in {home}: {', '.join(added)}")
    if removed:
        changes.append(f"gone from {home}: {', '.join(removed)}")
    for key, where in (("registry", "project-graphs/projects.json"),
                       ("index", "project-graphs/PROJECT-STATE-GRAPHS.md")):
        if before[key] != after[key]:
            changes.append(f"{where} lists {after[key]}, was {before[key]}")
    for table, was in before["ledger"].items():
        now = after["ledger"].get(table)
        if now != was:
            changes.append(f"orchestrator.db {table}: newest row {was} -> {now}")
    was, now = before.get("schema", {}), after.get("schema", {})
    if was and now:
        ran = sorted(set(now["migrations"]) - set(was["migrations"]))
        gone = sorted(set(was["migrations"]) - set(now["migrations"]))
        if ran:
            changes.append(f"orchestrator.db schema_version: migrations applied {', '.join(ran)}")
        if gone:
            changes.append(f"orchestrator.db schema_version: migrations gone {', '.join(gone)}")
        if was["journal_mode"] != now["journal_mode"]:
            changes.append(f"orchestrator.db journal_mode: {was['journal_mode']} -> {now['journal_mode']}")
    return changes


def main(argv: list[str]) -> int:
    if len(argv) != 3 or argv[1] not in ("snapshot", "check"):
        print(__doc__.split("\n\n")[1], file=sys.stderr)
        return 2
    path = Path(argv[2])
    if argv[1] == "snapshot":
        path.write_text(json.dumps(snapshot()))
        return 0
    changes = diff(json.loads(path.read_text()), snapshot())
    if not changes:
        return 0
    print("home_guard: written outside the sandbox:")
    for line in changes:
        print(f"  - {line}")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
