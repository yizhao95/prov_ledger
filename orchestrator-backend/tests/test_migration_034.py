"""Migration 034 — a supersede chain reads as its newest row (FL-238, FL-239).

change_reason_v gains `supersedes` (the ids a row corrects), node_badge_v stops
counting a superseded reason or rejected path, and the lookup is indexed. Only
views and an index change: every change_reason row is kept as it was."""
from pathlib import Path

import pytest

from orchestrator import db, provenance as pv

MIGRATIONS = Path(db.__file__).resolve().parent / "migrations"
LAST = "034_supersede_chains.sql"


@pytest.fixture
def at_033(tmp_path, monkeypatch):
    staged = tmp_path / "migrations_033"
    staged.mkdir()
    for f in sorted(MIGRATIONS.glob("*.sql")):
        if f.name < LAST:
            (staged / f.name).symlink_to(f)
    monkeypatch.setattr(db, "MIGRATIONS_DIR", staged)
    conn = db.open_db(tmp_path / "old.db")
    db.run_migrations(conn)
    monkeypatch.undo()
    old = pv.insert_reason(conn, project="p", plan_id="P", node_key="nk_a", kind="technical",
                           interpretation="the first reading", recorded_by="agent")
    new = pv.insert_reason(conn, project="p", plan_id="P", node_key="nk_a", kind="technical",
                           interpretation="the corrected reading", recorded_by="agent")
    pv.supersede(conn, old, new)
    rj = pv.insert_reason(conn, project="p", plan_id="P", node_key="nk_a", kind="technical", role="rejected_path",
                          interpretation="tried it", rule_id="R6", recorded_by="system")
    yield conn, {"old": old, "new": new, "rj": rj}
    conn.close()


def test_before_034_the_badge_counts_both_rows_of_a_chain(at_033):
    conn, _ = at_033
    assert tuple(conn.execute("SELECT reasons, rejected_paths FROM node_badge_v WHERE node_key = 'nk_a'").fetchone()) == (2, 1)


def test_034_adds_supersedes_and_counts_a_chain_once(at_033):
    conn, ids = at_033
    rows_before = [tuple(r) for r in conn.execute("SELECT * FROM change_reason ORDER BY id")]
    db.run_migrations(conn)
    assert [tuple(r) for r in conn.execute("SELECT * FROM change_reason ORDER BY id")] == rows_before
    sup = dict(conn.execute("SELECT id, supersedes FROM change_reason_v").fetchall())
    assert sup == {ids["old"]: None, ids["new"]: str(ids["old"]), ids["rj"]: None}
    assert tuple(conn.execute("SELECT reasons, rejected_paths, badge FROM node_badge_v WHERE node_key = 'nk_a'").fetchone()) == (1, 1, 2)
    assert conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = 'idx_change_reason_superseded_by'").fetchone()
    assert conn.execute("SELECT 1 FROM schema_version WHERE migration_file = ?", (LAST,)).fetchone()
