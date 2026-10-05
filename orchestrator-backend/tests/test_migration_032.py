"""Migration 032 — tool_call_log.failed (FL-208).

An ADD COLUMN on an append-only table: the rows already there read 0 (they were
all successes, because only PostToolUse was ever hooked), and the triggers that
refuse UPDATE and DELETE are untouched.
"""
import sqlite3
from pathlib import Path

import pytest

from orchestrator import db

MIGRATIONS = Path(db.__file__).resolve().parent / "migrations"
LAST = "032_tool_call_failed.sql"


@pytest.fixture
def at_031(tmp_path, monkeypatch):
    staged = tmp_path / "migrations_031"
    staged.mkdir()
    for f in sorted(MIGRATIONS.glob("*.sql")):
        if f.name < LAST:
            (staged / f.name).symlink_to(f)
    monkeypatch.setattr(db, "MIGRATIONS_DIR", staged)
    conn = db.open_db(tmp_path / "old.db")
    db.run_migrations(conn)
    monkeypatch.undo()
    conn.execute("INSERT INTO tool_call_log (session_id, cwd, tool_name) VALUES ('s', '/x', 'Read')")
    conn.commit()
    yield conn
    conn.close()


def test_the_migration_file_exists():
    assert (MIGRATIONS / LAST).is_file()


def test_old_rows_read_as_successes_and_new_rows_default_to_success(at_031):
    db.run_migrations(at_031)
    cols = {r[1]: r for r in at_031.execute("PRAGMA table_info(tool_call_log)")}
    assert "failed" in cols
    assert [tuple(r) for r in at_031.execute("SELECT failed FROM tool_call_log")] == [(0,)]
    at_031.execute("INSERT INTO tool_call_log (session_id, cwd, tool_name) VALUES ('s', '/x', 'Bash')")
    assert tuple(at_031.execute("SELECT failed FROM tool_call_log ORDER BY id DESC LIMIT 1").fetchone()) == (0,)


def test_the_log_is_still_append_only(at_031):
    db.run_migrations(at_031)
    with pytest.raises(sqlite3.DatabaseError):
        at_031.execute("UPDATE tool_call_log SET failed = 1")
    with pytest.raises(sqlite3.DatabaseError):
        at_031.execute("DELETE FROM tool_call_log")
