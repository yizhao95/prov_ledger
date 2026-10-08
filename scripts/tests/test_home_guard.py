"""Tests for scripts/home_guard.py — did a test suite write to the real ~/skill-workspace?

The guard has to tell a leaking test apart from the provLedger hooks of the
Claude Code session the suites are run from: those hooks write tool calls and
prompts into the real ledger, and the Stop hook rewrites the project's graph and
its index, all while the suites run. A guard that went red on those would be
ignored within a day. So it checks what a leaking test changes and a hook does
not: which projects the registry and its index list, the newest row of the
tables only plans and questions write, the ledger's applied migrations and
journal mode, and new entries at the top of the workspace.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
GUARD = REPO / "scripts" / "home_guard.py"


def _workspace(tmp: Path) -> Path:
    ws = tmp / "skill-workspace"
    graphs = ws / "project-graphs"
    graphs.mkdir(parents=True)
    _write_registry(graphs, ["prov_ledger"])
    conn = sqlite3.connect(ws / "orchestrator.db")
    conn.executescript("""
        CREATE TABLE Plans (id TEXT PRIMARY KEY);
        CREATE TABLE Steps (id TEXT PRIMARY KEY);
        CREATE TABLE change_reason (id INTEGER PRIMARY KEY, plan_id TEXT);
        CREATE TABLE tool_call_log (id INTEGER PRIMARY KEY);
        CREATE TABLE utterance (id INTEGER PRIMARY KEY);
        INSERT INTO Plans VALUES ('p1');
        INSERT INTO change_reason VALUES (1, 'p1');
    """)
    conn.commit()
    conn.close()
    (ws / "hook-errors.log").write_text("")
    return ws


def _write_registry(graphs: Path, names: list[str]) -> None:
    (graphs / "projects.json").write_text(json.dumps(
        {"projects": [{"name": n, "db_path": f"{graphs}/{n}/{n}-state-graph.db"} for n in names]}))
    rows = "\n".join(f"| {n} | /repo/{n} | `x.db` | abc | now |" for n in names)
    (graphs / "PROJECT-STATE-GRAPHS.md").write_text(
        "# Project State Graphs — Index\n\n| Project | Repo | Deep graph (sqlite) | Commit | Updated |\n"
        f"|---|---|---|---|---|\n{rows}\n")


def _guard(ws: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(GUARD), *args], capture_output=True, text=True,
                          env={**os.environ, "PROVLEDGER_GUARD_HOME": str(ws)})


def _snapshot(ws: Path, tmp: Path) -> Path:
    snap = tmp / "snap.json"
    proc = _guard(ws, "snapshot", str(snap))
    assert proc.returncode == 0, proc.stderr
    return snap


def test_nothing_changed_passes(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    snap = _snapshot(ws, tmp_path)
    assert _guard(ws, "check", str(snap)).returncode == 0


def test_what_the_hooks_write_is_not_a_leak(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    snap = _snapshot(ws, tmp_path)
    conn = sqlite3.connect(ws / "orchestrator.db")
    conn.execute("INSERT INTO tool_call_log VALUES (NULL)")
    conn.execute("INSERT INTO utterance VALUES (NULL)")
    # the Stop hook's degraded mode closes the session with reasons of its own
    conn.execute("INSERT INTO change_reason VALUES (NULL, 'session:abc')")
    conn.commit()
    conn.close()
    with open(ws / "hook-errors.log", "a") as fh:
        fh.write("PostToolUse OperationalError: database is locked\n")
    _write_registry(ws / "project-graphs", ["prov_ledger"])          # a Stop-hook refresh
    (ws / "project-graphs" / "prov_ledger").mkdir()
    proc = _guard(ws, "check", str(snap))
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_a_project_dropped_from_the_index_is_a_leak(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    snap = _snapshot(ws, tmp_path)
    graphs = ws / "project-graphs"
    (graphs / "PROJECT-STATE-GRAPHS.md").write_text(
        "| Project | Repo |\n|---|---|\n| demo | /tmp/demo |\n")       # regenerated from a test registry
    proc = _guard(ws, "check", str(snap))
    assert proc.returncode == 1
    assert "PROJECT-STATE-GRAPHS.md" in proc.stdout and "prov_ledger" in proc.stdout


def test_a_project_added_to_the_registry_is_a_leak(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    snap = _snapshot(ws, tmp_path)
    _write_registry(ws / "project-graphs", ["prov_ledger", "dummy-rollup"])
    proc = _guard(ws, "check", str(snap))
    assert proc.returncode == 1
    assert "dummy-rollup" in proc.stdout


def test_a_plan_written_to_the_real_ledger_is_a_leak(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    snap = _snapshot(ws, tmp_path)
    conn = sqlite3.connect(ws / "orchestrator.db")
    conn.execute("INSERT INTO Plans VALUES ('test-plan')")
    conn.commit()
    conn.close()
    proc = _guard(ws, "check", str(snap))
    assert proc.returncode == 1
    assert "Plans" in proc.stdout


def test_a_new_entry_in_the_workspace_is_a_leak(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    snap = _snapshot(ws, tmp_path)
    (ws / "provledger-extensions.json").write_text("{}")
    proc = _guard(ws, "check", str(snap))
    assert proc.returncode == 1
    assert "provledger-extensions.json" in proc.stdout


def test_creating_the_workspace_is_a_leak_and_its_absence_is_fine(tmp_path: Path) -> None:
    ws = tmp_path / "skill-workspace"
    snap = _snapshot(ws, tmp_path)
    assert _guard(ws, "check", str(snap)).returncode == 0
    (ws / "project-graphs").mkdir(parents=True)
    assert _guard(ws, "check", str(snap)).returncode == 1


def test_a_reason_written_outside_a_session_is_a_leak(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    snap = _snapshot(ws, tmp_path)
    conn = sqlite3.connect(ws / "orchestrator.db")
    conn.execute("INSERT INTO change_reason VALUES (NULL, NULL)")       # e.g. `provledger note` with no ORCH_DB
    conn.commit()
    conn.close()
    proc = _guard(ws, "check", str(snap))
    assert proc.returncode == 1
    assert "change_reason" in proc.stdout


def test_the_release_check_uses_this_guard_not_file_hashes() -> None:
    """release-e2e.sh used to fail when the registry's or the index's sha256
    changed during the run, and the Stop hook of the session that launched it
    rewrites both on every graph refresh (the prov_ledger row's commit and time).
    One guard, one definition of a leak."""
    src = (REPO / "scripts" / "release-e2e.sh").read_text()
    assert "home_guard.py\" snapshot" in src and "home_guard.py\" check" in src
    assert "GUARD_REG_BEFORE" not in src and "GUARD_IDX_BEFORE" not in src


def test_sqlite_sidecar_files_are_not_a_leak(tmp_path: Path) -> None:
    """In rollback-journal mode SQLite creates orchestrator.db-journal for the
    length of one write transaction — the session's own hooks write the real
    ledger all the time, so a snapshot or a check can catch one."""
    ws = _workspace(tmp_path)
    (ws / "orchestrator.db-journal").write_text("")      # present at snapshot time
    snap = _snapshot(ws, tmp_path)
    (ws / "orchestrator.db-journal").unlink()
    (ws / "orchestrator.db-wal").write_text("")
    (ws / "orchestrator.db-shm").write_text("")
    proc = _guard(ws, "check", str(snap))
    assert proc.returncode == 0, proc.stdout
    (ws / "notes.txt").write_text("")                     # anything else still counts
    assert _guard(ws, "check", str(snap)).returncode == 1


def test_the_hooks_spool_is_not_a_leak(tmp_path: Path) -> None:
    """FL-193: a hook that cannot get the write lock appends its row to
    orchestrator.db.spool.jsonl, and the next hook moves it aside to
    orchestrator.db.spool.jsonl.<pid> while it replays — the session's own
    hooks, writing the real ledger, not a test."""
    ws = _workspace(tmp_path)
    snap = _snapshot(ws, tmp_path)
    (ws / "orchestrator.db.spool.jsonl").write_text("{}\n")
    (ws / "orchestrator.db.spool.jsonl.4242").write_text("{}\n")
    proc = _guard(ws, "check", str(snap))
    assert proc.returncode == 0, proc.stdout


def test_a_migration_applied_to_the_real_ledger_is_a_leak(tmp_path: Path) -> None:
    """FL-237: a test that opened the real ledger with the working tree's code
    ran a development branch's migration on it, and every row count stayed the
    same. A migration that has run anywhere is frozen, so a wrong one would be
    frozen on the real ledger. The hooks run the installed release, whose
    migrations are already applied, so they never add one."""
    ws = _workspace(tmp_path)
    conn = sqlite3.connect(ws / "orchestrator.db")
    conn.executescript("""
        CREATE TABLE schema_version (version INTEGER PRIMARY KEY, migration_file TEXT);
        INSERT INTO schema_version VALUES (1, '001_init.sql');
    """)
    conn.close()
    snap = _snapshot(ws, tmp_path)
    conn = sqlite3.connect(ws / "orchestrator.db")
    conn.execute("INSERT INTO schema_version VALUES (2, '002_next.sql')")
    conn.commit()
    conn.close()
    proc = _guard(ws, "check", str(snap))
    assert proc.returncode == 1
    assert "schema_version" in proc.stdout and "002_next.sql" in proc.stdout


def test_switching_the_real_ledger_to_wal_is_a_leak(tmp_path: Path) -> None:
    """FL-237: opening a ledger with the working tree's `db.open_db` switches it
    to WAL; the real ledger was in WAL before the release that does so was
    installed."""
    ws = _workspace(tmp_path)
    snap = _snapshot(ws, tmp_path)
    conn = sqlite3.connect(ws / "orchestrator.db")
    assert conn.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    conn.close()
    proc = _guard(ws, "check", str(snap))
    assert proc.returncode == 1
    assert "journal_mode" in proc.stdout and "delete" in proc.stdout and "wal" in proc.stdout


def test_a_legacy_migration_row_without_a_file_name_is_read_and_not_a_leak(tmp_path: Path) -> None:
    """The real ledger's schema_version starts with a row from before the table
    had a migration_file column (version 1, NULL). The guard must read past it,
    and still see a migration applied after the snapshot."""
    ws = _workspace(tmp_path)
    conn = sqlite3.connect(ws / "orchestrator.db")
    conn.executescript("""
        CREATE TABLE schema_version (version INTEGER PRIMARY KEY, migration_file TEXT);
        INSERT INTO schema_version VALUES (1, NULL), (2, '001_init.sql');
    """)
    conn.close()
    snap = _snapshot(ws, tmp_path)
    proc = _guard(ws, "check", str(snap))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    conn = sqlite3.connect(ws / "orchestrator.db")
    conn.execute("INSERT INTO schema_version VALUES (3, '002_next.sql')")
    conn.commit()
    conn.close()
    proc = _guard(ws, "check", str(snap))
    assert proc.returncode == 1 and "002_next.sql" in proc.stdout
