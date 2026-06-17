"""Tests for db.py — connection, migrations, CRUD."""
import sqlite3
from pathlib import Path

import pytest

from orchestrator import db


def test_open_db_creates_file(tmp_path: Path):
    p = tmp_path / "new.db"
    assert not p.exists()
    c = db.open_db(p)
    assert p.exists()
    c.close()


def test_open_db_enables_foreign_keys(conn):
    fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    assert fk == 1


def test_run_migrations_idempotent(tmp_path: Path):
    p = tmp_path / "m.db"
    c = db.open_db(p)
    n1 = db.run_migrations(c)
    n2 = db.run_migrations(c)
    # First run applies all migration files in migrations/ (count grows over time).
    # Second run must apply ZERO — idempotency property. The pre-2026-05-26
    # runner had a chicken-and-egg bug that made this assert n1==n2==1, masking
    # the fact that NO migration was recorded with its filename, so every
    # subsequent run re-applied everything and exploded on duplicate-column.
    # See migration-006 work for the fix.
    assert n1 >= 1, f"first run should apply >=1 migration, got {n1}"
    assert n2 == 0, f"second run must be a no-op (idempotent), got {n2}"
    # tables should exist
    rows = c.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    names = [r["name"] for r in rows]
    assert "Plans" in names and "Steps" in names and "schema_version" in names


def test_insert_get_plan_roundtrip(conn):
    db.insert_plan(conn, "p1", "test goal", max_revisions=7)
    plan = db.get_plan(conn, "p1")
    assert plan["plan_id"] == "p1"
    assert plan["original_goal"] == "test goal"
    assert plan["status"] == "IN_PROGRESS"
    assert plan["max_revisions"] == 7
    assert plan["revision_count"] == 0


def test_get_plan_returns_none_for_missing(conn):
    assert db.get_plan(conn, "ghost") is None


def test_insert_get_steps_ordering(conn):
    db.insert_plan(conn, "p1", "g")
    db.insert_step(conn, "p1-C", "p1", "third", execution_order=2)
    db.insert_step(conn, "p1-A", "p1", "first", execution_order=0)
    db.insert_step(conn, "p1-B", "p1", "second", execution_order=1)
    steps = db.get_steps(conn, "p1")
    assert [s["step_id"] for s in steps] == ["p1-A", "p1-B", "p1-C"]


def test_foreign_key_enforced(conn):
    """Inserting a step with bogus plan_id must fail."""
    with pytest.raises(sqlite3.IntegrityError):
        db.insert_step(conn, "bad", "ghost-plan", "x", execution_order=0)


def test_update_step_status_sets_timestamps(conn):
    db.insert_plan(conn, "p1", "g")
    db.insert_step(conn, "p1-A", "p1", "step", execution_order=0)
    db.update_step_status(conn, "p1-A", "IN_PROGRESS", set_started=True)
    s = db.get_step(conn, "p1-A")
    assert s["status"] == "IN_PROGRESS"
    assert s["started_at"] is not None
    assert s["completed_at"] is None

    db.update_step_status(conn, "p1-A", "COMPLETED", set_completed=True)
    s = db.get_step(conn, "p1-A")
    assert s["status"] == "COMPLETED"
    assert s["completed_at"] is not None


def test_increment_revision(conn):
    db.insert_plan(conn, "p1", "g")
    assert db.increment_revision(conn, "p1") == 1
    assert db.increment_revision(conn, "p1") == 2
    assert db.get_plan(conn, "p1")["revision_count"] == 2


def test_get_children(conn):
    db.insert_plan(conn, "p1", "g")
    db.insert_step(conn, "p1-A", "p1", "parent", execution_order=0)
    db.insert_step(conn, "p1-A.1", "p1", "child1", execution_order=1, parent_step_id="p1-A", depth_level=1)
    db.insert_step(conn, "p1-A.2", "p1", "child2", execution_order=2, parent_step_id="p1-A", depth_level=1)
    kids = db.get_children(conn, "p1-A")
    assert [k["step_id"] for k in kids] == ["p1-A.1", "p1-A.2"]


def test_status_check_constraint(conn):
    """SQLite CHECK constraint should reject invalid status values."""
    db.insert_plan(conn, "p1", "g")
    with pytest.raises(sqlite3.IntegrityError):
        db.insert_step(conn, "p1-A", "p1", "step", execution_order=0, status="WIBBLE")
