"""027 — export_log: what left the machine, and what was refused.

An export is the one operation with no undo. The row it writes is therefore
append-only like the three chains: no UPDATE, no DELETE. `included_rationale_json`
is the column this table exists for — a released rationale is on the record by
reason id, so "who decided this could leave" has an answer.
"""
import sqlite3

import pytest

COLUMNS = {"id", "project", "out_dir", "fmt", "included_rationale_json", "counts_json",
           "skipped_json", "chain_heads_json", "anchor_note_sha", "anchor_commit",
           "manifest_sha256", "exported_by", "exported_at"}


def _cols(conn) -> set:
    return {r[1] for r in conn.execute("PRAGMA table_info(export_log)")}


def _row(conn, **over):
    row = dict(project="demo", out_dir="/tmp/bundle", fmt="md", included_rationale_json="[7]",
               counts_json='{"change_reason": 3}', skipped_json='{"personal_statement": 1}',
               chain_heads_json='{"change_reason": {"id": 9, "hash": "abc"}}',
               anchor_note_sha="n1", anchor_commit="c1", manifest_sha256="m1", exported_by="agent")
    row.update(over)
    cols = ", ".join(row)
    conn.execute(f"INSERT INTO export_log ({cols}) VALUES ({', '.join('?' * len(row))})", tuple(row.values()))
    conn.commit()
    return conn.execute("SELECT MAX(id) FROM export_log").fetchone()[0]


def test_027_creates_export_log_with_every_column_the_feature_needs(conn):
    assert _cols(conn) == COLUMNS


def test_the_export_time_is_the_databases_own_clock(conn):
    rid = _row(conn)
    at = conn.execute("SELECT exported_at FROM export_log WHERE id = ?", (rid,)).fetchone()[0]
    assert at and len(at) == 19          # 'YYYY-MM-DD HH:MM:SS', written by the DB, not the caller


def test_an_export_that_happened_cannot_be_edited_or_deleted(conn):
    rid = _row(conn)
    for col, value in (("project", "other"), ("out_dir", "/elsewhere"),
                       ("included_rationale_json", "[]"), ("skipped_json", "{}")):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(f"UPDATE export_log SET {col} = ? WHERE id = ?", (value, rid))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM export_log WHERE id = ?", (rid,))


def test_exported_by_is_the_three_who_can_act(conn):
    for by in ("human", "agent", "system"):
        _row(conn, exported_by=by)
    with pytest.raises(sqlite3.IntegrityError):
        _row(conn, exported_by="somebody")


def test_running_the_migration_twice_changes_nothing(conn):
    from orchestrator import db
    rid = _row(conn)
    db.run_migrations(conn)
    assert _cols(conn) == COLUMNS
    assert conn.execute("SELECT COUNT(*) FROM export_log").fetchone()[0] == 1
    assert conn.execute("SELECT id FROM export_log").fetchone()[0] == rid
