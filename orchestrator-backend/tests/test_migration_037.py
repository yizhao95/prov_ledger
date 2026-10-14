"""Migration 037 — evidence follows the task, and a source keeps its words
(task-level redesign, step 3b).

A reference was a pointer only: a label, a date, a uri. The redesign keeps the
original words too — who said it and what they said — because the root of a
task is usually a sentence in a meeting or a mail, and the pointer alone rots.
Two nullable columns on `reference`, both inside the hash chain for new rows;
rows written before 037 verify in the form they were hashed in.

`evidence_log` recorded one ending per changed node. Evidence is looked for once
per task now, so a row may name the task reason instead of a node. SQLite cannot
loosen NOT NULL in place, so the table is rebuilt with every row kept."""
import sqlite3
from pathlib import Path

import pytest

from orchestrator import db, provenance as pv

MIGRATIONS = Path(db.__file__).resolve().parent / "migrations"
LAST = "037_task_evidence.sql"


@pytest.fixture
def at_036(tmp_path, monkeypatch):
    staged = tmp_path / "migrations_036"
    staged.mkdir()
    for f in sorted(MIGRATIONS.glob("*.sql")):
        if f.name < LAST:
            (staged / f.name).symlink_to(f)
    monkeypatch.setattr(db, "MIGRATIONS_DIR", staged)
    conn = db.open_db(tmp_path / "old.db")
    db.run_migrations(conn)
    monkeypatch.undo()
    # written the way the store wrote a reference before 037: no said_by, no
    # excerpt, hashed without them
    refs = [pv._insert_chained(conn, "reference", {
                "project": "p", "kind": kind, "uri": uri, "label": label, "occurred_at": at,
                "registered_at": "2026-05-21 09:00:00", "verifiability": ver, "visibility": "shareable",
                "last_checked": None})
            for kind, uri, label, at, ver in (
                ("email", "mailto:platform@example.invalid", "Re: the feed", "2026-05-20 11:20", "linked"),
                ("verbal", None, "said at stand-up", "2026-05-21", "verbal"))]
    conn.commit()
    # and two evidence_log rows the way the store wrote them before 037: per node
    logs = [conn.execute("INSERT INTO evidence_log (plan_id, node_key, searched, tool_hint, outcome) "
                         "VALUES ('P', ?, ?, ?, ?)", row).lastrowid
            for row in (("nk_a", 1, "mail", "found_nothing"), ("nk_b", 0, None, "not_searched"))]
    conn.commit()
    yield conn, refs, logs
    conn.close()


def _cols(conn, table):
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]


def test_037_adds_said_by_and_excerpt_and_old_rows_read_null(at_036):
    conn, refs, _ = at_036
    before = [tuple(r) for r in conn.execute("SELECT * FROM reference ORDER BY id")]
    db.run_migrations(conn)
    assert {"said_by", "excerpt"} <= set(_cols(conn, "reference"))
    after = [dict(r) for r in conn.execute("SELECT * FROM reference ORDER BY id")]
    assert [r["id"] for r in after] == refs
    assert all(r["said_by"] is None and r["excerpt"] is None for r in after)
    old_cols = [c for c in _cols(conn, "reference") if c not in ("said_by", "excerpt")]
    assert [tuple(r[c] for c in old_cols) for r in after] == before


def test_the_chain_verifies_old_rows_in_their_own_form_and_new_rows_in_the_current_one(at_036):
    conn, refs, _ = at_036
    db.run_migrations(conn)
    rid = pv.insert_reference(conn, project="p", kind="meeting", label="weekly sync, 2026-05-19",
                              occurred_at="2026-05-19 15:00", uri="notes/2026-05-19-sync.md",
                              said_by="the platform lead",
                              excerpt="From the June release the v2 feed no longer carries orders.discount.")
    row = dict(conn.execute("SELECT * FROM reference WHERE id = ?", (rid,)).fetchone())
    assert (row["said_by"], row["excerpt"][:10]) == ("the platform lead", "From the J")
    v = pv.verify_chain(conn, "reference")
    assert v["ok"] is True and v["rows"] == 3 and v["older_form"] == 2


def test_a_tampered_old_reference_still_fails_the_chain(at_036):
    conn, refs, _ = at_036
    db.run_migrations(conn)
    conn.execute("DROP TRIGGER trg_reference_update_whitelist")
    conn.execute("UPDATE reference SET label = 'Re: something else' WHERE id = ?", (refs[0],))
    v = pv.verify_chain(conn, "reference")
    assert v["ok"] is False and v["first_bad_id"] == refs[0]


def test_the_whitelist_still_lets_only_last_checked_change(at_036):
    conn, refs, _ = at_036
    db.run_migrations(conn)
    for col, value in (("said_by", "someone"), ("excerpt", "words added later")):
        with pytest.raises(sqlite3.IntegrityError, match="only last_checked"):
            conn.execute(f"UPDATE reference SET {col} = ? WHERE id = ?", (value, refs[0]))
    conn.execute("UPDATE reference SET last_checked = '2026-10-13' WHERE id = ?", (refs[0],))


@pytest.mark.parametrize("col, n", [("excerpt", 2001), ("said_by", 201), ("excerpt", 0), ("said_by", 0)])
def test_the_columns_have_bounds_in_the_schema(at_036, col, n):
    conn, _, _ = at_036
    db.run_migrations(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(f"INSERT INTO reference (project, kind, label, occurred_at, verifiability, {col}, hash) "
                     "VALUES ('p', 'doc', 'x', '2026-01-01', 'unreachable', ?, 'h')", ("w" * n,))


def test_insert_reference_names_the_limit_before_the_schema_does(at_036):
    conn, _, _ = at_036
    db.run_migrations(conn)
    with pytest.raises(ValueError, match="2000"):
        pv.insert_reference(conn, project="p", kind="doc", label="x", occurred_at="2026-01-01", excerpt="w" * 2001)
    with pytest.raises(ValueError, match="200"):
        pv.insert_reference(conn, project="p", kind="doc", label="x", occurred_at="2026-01-01", said_by="w" * 201)


def test_037_rebuilds_evidence_log_so_a_row_may_name_the_task_reason(at_036):
    conn, _, logs = at_036
    before = [tuple(r) for r in conn.execute("SELECT * FROM evidence_log ORDER BY id")]
    db.run_migrations(conn)
    assert "reason_id" in _cols(conn, "evidence_log")
    after = [dict(r) for r in conn.execute("SELECT * FROM evidence_log ORDER BY id")]
    assert [r["id"] for r in after] == logs and all(r["reason_id"] is None for r in after)
    old_cols = [c for c in _cols(conn, "evidence_log") if c != "reason_id"]
    assert [tuple(r[c] for c in old_cols) for r in after] == before
    task = pv.insert_reason(conn, project="p", plan_id="P", node_key=None, kind="technical",
                            interpretation="the feed dropped the column", recorded_by="agent")
    conn.execute("INSERT INTO evidence_log (plan_id, reason_id, searched, outcome) VALUES ('P', ?, 1, 'attached')", (task,))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO evidence_log (plan_id, searched, outcome) VALUES ('P', 1, 'attached')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO evidence_log (plan_id, reason_id, searched, outcome) VALUES ('P', ?, 1, 'not_searched')", (task,))
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE evidence_log SET outcome = 'attached'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM evidence_log")
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
