"""Migration 036 — a reason may belong to the task, not to a node (task-level
redesign, step 3a).

change_reason was created (018) with `CHECK (node_key IS NOT NULL OR role =
'rejected_path')`: every reason had to hang on a node. A task's reason belongs
to the plan; its nodes reach it through the state graph's history. SQLite cannot
loosen a CHECK in place, so the table is rebuilt: every row kept with its id and
hash (the chain still verifies), the indexes, the two triggers and the four views
that read it recreated, and the four tables that point at it still pointing at
it. Two small append-only tables come with it: reason_node (which task reason a
node belongs to, when a plan did several things) and plan_bundle (why those
things share a plan)."""
import sqlite3
from pathlib import Path

import pytest

from orchestrator import db, provenance as pv

MIGRATIONS = Path(db.__file__).resolve().parent / "migrations"
LAST = "036_task_reasons.sql"
VIEWS = ("change_reason_v", "node_badge_v", "node_reason_v", "reason_stats_v")


@pytest.fixture
def at_035(tmp_path, monkeypatch):
    staged = tmp_path / "migrations_035"
    staged.mkdir()
    for f in sorted(MIGRATIONS.glob("*.sql")):
        if f.name < LAST:
            (staged / f.name).symlink_to(f)
    monkeypatch.setattr(db, "MIGRATIONS_DIR", staged)
    conn = db.open_db(tmp_path / "old.db")
    db.run_migrations(conn)
    monkeypatch.undo()
    u = pv.insert_utterance(conn, session_id="s", project="p", plan_id="P", text="keep the weekly grain",
                            occurred_at="2026-09-15 10:00:00")
    ids = {
        "stated": pv.insert_reason(conn, project="p", plan_id="P", node_key="nk_a", kind="technical",
                                   verbatim=(u, 0, 10), recorded_by="agent"),
        "asserted": pv.insert_reason(conn, project="p", plan_id="P", node_key="nk_b", kind="technical",
                                     interpretation="the feed dropped the column", recorded_by="agent"),
        "floating": pv.insert_reason(conn, project="p", plan_id="P", node_key=None, kind="technical",
                                     role="rejected_path", interpretation="tried the archive", rule_id="R6",
                                     recorded_by="system"),
    }
    conn.execute("INSERT INTO read_hit (reason_id, project, plan_id, moment) VALUES (?, 'p', 'P', 'plan')", (ids["asserted"],))
    conn.commit()
    yield conn, ids
    conn.close()


def _schema_objects(conn, kind):
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = ? AND tbl_name = 'change_reason'", (kind,))}


def test_before_036_a_reason_must_hang_on_a_node(at_035):
    conn, _ = at_035
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO change_reason (project, plan_id, kind, role, occurred_at, recorded_by, tier, hash) "
                     "VALUES ('p', 'P', 'technical', 'reason', '2026-09-15', 'agent', 'unstated', 'h')")


def test_036_keeps_every_row_and_hash_and_the_chain_still_verifies(at_035):
    conn, ids = at_035
    before = [tuple(r) for r in conn.execute("SELECT * FROM change_reason ORDER BY id")]
    views_before = {v: [tuple(r) for r in conn.execute(f"SELECT * FROM {v} ORDER BY 1")] for v in VIEWS}
    indexes, triggers = _schema_objects(conn, "index"), _schema_objects(conn, "trigger")
    db.run_migrations(conn)
    assert [tuple(r) for r in conn.execute("SELECT * FROM change_reason ORDER BY id")] == before
    assert pv.verify_chain(conn, "change_reason")["ok"] is True
    for v in VIEWS:
        assert [tuple(r) for r in conn.execute(f"SELECT * FROM {v} ORDER BY 1")] == views_before[v], v
    assert _schema_objects(conn, "index") >= indexes and _schema_objects(conn, "trigger") == triggers
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert conn.execute("SELECT reason_id FROM read_hit").fetchone()[0] == ids["asserted"]


def test_after_036_a_task_reason_has_no_node_but_a_constraint_still_needs_one(at_035):
    conn, _ = at_035
    db.run_migrations(conn)
    cur = conn.execute("INSERT INTO change_reason (project, plan_id, kind, role, interpretation, occurred_at, recorded_by, tier, hash) "
                       "VALUES ('p', 'P', 'technical', 'reason', 'the reviewer fixed the evaluation window', '2026-09-15', 'agent', 'asserted', 'h')")
    rid = cur.lastrowid
    assert tuple(conn.execute("SELECT node_key, role, tier FROM change_reason WHERE id = ?", (rid,)).fetchone()) == (None, "reason", "asserted")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO change_reason (project, plan_id, kind, role, statement, occurred_at, recorded_by, tier, hash) "
                     "VALUES ('p', 'P', 'organizational', 'constraint', 'keep it', '2026-09-15', 'human', 'asserted', 'h')")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM change_reason WHERE id = ?", (rid,))
    with pytest.raises(sqlite3.IntegrityError, match="only superseded_by"):
        conn.execute("UPDATE change_reason SET interpretation = 'x' WHERE id = ?", (rid,))


def test_036_adds_reason_node_and_plan_bundle_as_append_only_tables(at_035):
    conn, ids = at_035
    db.run_migrations(conn)
    conn.execute("INSERT INTO reason_node (reason_id, node_key) VALUES (?, 'nk_a')", (ids["asserted"],))
    conn.execute("INSERT INTO plan_bundle (plan_id, basis, recorded_by) VALUES ('P', 'both are close-time fixes', 'agent')")
    for table in ("reason_node", "plan_bundle"):
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(f"UPDATE {table} SET id = id")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(f"DELETE FROM {table}")
