"""Migration 033 — a plan can be ABANDONED (FL-216).

Plans.status was CHECKed to IN_PROGRESS / COMPLETED / FAILED, so a plan nobody
started had no honest end: finish-plan would call it COMPLETED. SQLite cannot
widen a CHECK in place, so Plans is rebuilt (the way 007 rebuilt Steps): every
row and column kept, the other CHECKs kept, the two indexes recreated, and the
three tables that point at Plans still pointing at it.
"""
import sqlite3
from pathlib import Path

import pytest

from orchestrator import db

MIGRATIONS = Path(db.__file__).resolve().parent / "migrations"
LAST = "033_plan_abandoned.sql"


@pytest.fixture
def at_032(tmp_path, monkeypatch):
    staged = tmp_path / "migrations_032"
    staged.mkdir()
    for f in sorted(MIGRATIONS.glob("*.sql")):
        if f.name < LAST:
            (staged / f.name).symlink_to(f)
    monkeypatch.setattr(db, "MIGRATIONS_DIR", staged)
    conn = db.open_db(tmp_path / "old.db")
    db.run_migrations(conn)
    monkeypatch.undo()
    db.insert_plan(conn, "p1", "goal one", user_query="do it", project="proj", project_source="declared")
    db.insert_step(conn, "p1-A", "p1", "TEST: a", execution_order=0, step_type="CODE")
    db.add_skill_activation(conn, plan_id="p1", skill_name="writing-plans", source="iron-law")
    db.insert_deviation(conn, "p1", None, "[BUDGET RAISED] 5 -> 6: why", new_step_ids=[], revision_count=0)
    conn.execute("UPDATE Plans SET review_state = 'awaiting_agent', session_id = 's1', headline_json = '{}' WHERE plan_id = 'p1'")
    conn.commit()
    yield conn
    conn.close()


def test_the_migration_file_exists():
    assert (MIGRATIONS / LAST).is_file()


def test_every_row_and_column_survives(at_032):
    before = [tuple(r) for r in at_032.execute("SELECT * FROM Plans")]
    db.run_migrations(at_032)
    assert [tuple(r) for r in at_032.execute("SELECT * FROM Plans")] == before


def test_a_plan_can_be_abandoned_and_the_other_checks_still_hold(at_032):
    db.run_migrations(at_032)
    at_032.execute("UPDATE Plans SET status = 'ABANDONED' WHERE plan_id = 'p1'")
    with pytest.raises(sqlite3.IntegrityError):
        at_032.execute("UPDATE Plans SET status = 'MAYBE' WHERE plan_id = 'p1'")
    with pytest.raises(sqlite3.IntegrityError):
        at_032.execute("UPDATE Plans SET review_state = 'whenever' WHERE plan_id = 'p1'")
    with pytest.raises(sqlite3.IntegrityError):
        at_032.execute("UPDATE Plans SET project_source = 'guess' WHERE plan_id = 'p1'")


def test_the_indexes_and_the_foreign_keys_are_intact(at_032):
    db.run_migrations(at_032)
    names = {r[0] for r in at_032.execute("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='Plans'")}
    assert {"idx_plans_project", "idx_plans_session"} <= names
    assert at_032.execute("PRAGMA foreign_key_check").fetchall() == []
    for child in ("Steps", "SkillActivations", "Deviations"):
        assert "Plans" in {r[2] for r in at_032.execute(f"PRAGMA foreign_key_list({child})")}, child
    db.insert_step(at_032, "p1-B", "p1", "CODE: b", execution_order=1, step_type="CODE")
    with pytest.raises(sqlite3.IntegrityError):
        db.insert_step(at_032, "nope-A", "no-such-plan", "x", execution_order=0)
