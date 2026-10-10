"""Migration 035 — a plan's root cause (task-level redesign, step 2).

A plan is a task, and why it exists usually started earlier: the user said
what they wanted once, and several plans carry it forward. `plan_root` records,
per plan, whether it starts a new root, continues an earlier plan's, or nobody
said. Each judgement and each later confirmation is a new row (the latest one
counts), so the table is append-only like the other provenance logs."""
import sqlite3
from pathlib import Path

import pytest

from orchestrator import db

MIGRATIONS = Path(db.__file__).resolve().parent / "migrations"
LAST = "035_plan_root.sql"


@pytest.fixture
def at_034(tmp_path, monkeypatch):
    staged = tmp_path / "migrations_034"
    staged.mkdir()
    for f in sorted(MIGRATIONS.glob("*.sql")):
        if f.name < LAST:
            (staged / f.name).symlink_to(f)
    monkeypatch.setattr(db, "MIGRATIONS_DIR", staged)
    conn = db.open_db(tmp_path / "old.db")
    db.run_migrations(conn)
    monkeypatch.undo()
    db.insert_plan(conn, "p1", "goal one", user_query="do it", project="proj", project_source="declared")
    conn.commit()
    yield conn
    conn.close()


def _row(conn, **kw):
    cols = ", ".join(kw)
    conn.execute(f"INSERT INTO plan_root ({cols}) VALUES ({', '.join('?' * len(kw))})", tuple(kw.values()))


def test_035_creates_an_append_only_plan_root_and_keeps_the_plans(at_034):
    conn = at_034
    plans_before = [tuple(r) for r in conn.execute("SELECT * FROM Plans")]
    db.run_migrations(conn)
    assert [tuple(r) for r in conn.execute("SELECT * FROM Plans")] == plans_before
    cols = {r[1] for r in conn.execute("PRAGMA table_info(plan_root)")}
    assert {"plan_id", "kind", "continues_plan_id", "root_plan_id", "utterance_id", "basis", "state",
            "recorded_by", "at"} <= cols
    idx = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='plan_root'")}
    assert {"idx_plan_root_plan", "idx_plan_root_root"} <= idx
    _row(conn, plan_id="p1", kind="new", root_plan_id="p1", state="asserted", recorded_by="agent")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE plan_root SET state = 'confirmed'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM plan_root")


@pytest.mark.parametrize("row", [
    {"kind": "continues", "root_plan_id": "p0"},                         # continues names no plan
    {"kind": "new", "continues_plan_id": "p0", "root_plan_id": "p1"},    # only continues names one
    {"kind": "unknown", "root_plan_id": "p1"},                           # unknown has no root
    {"kind": "maybe", "root_plan_id": "p1"},
    {"kind": "new", "root_plan_id": "p1", "state": "guessed"},
    {"kind": "new", "root_plan_id": "p1", "recorded_by": "model"},
])
def test_the_checks_refuse_a_row_that_says_two_things(at_034, row):
    conn = at_034
    db.run_migrations(conn)
    full = {"plan_id": "p1", "state": "asserted", "recorded_by": "agent", **row}
    with pytest.raises(sqlite3.IntegrityError):
        _row(conn, **full)
