"""The ledger runs in WAL (FL-193).

In SQLite's default rollback journal a reader blocks a writer: the dashboard's
two-second poll, a long `ask`, a plan close that reads the graph inside its
write transaction — any of them made a hook wait past its busy timeout, give up,
and drop the user's words or a tool call. In WAL readers and the writer do not
block each other.

The switch happens in `db.open_db`, not in a migration: SQLite answers a
`journal_mode=WAL` it cannot apply (another connection is reading) by quietly
returning the old mode, and a migration runs once — it would be recorded as
applied with the ledger still in rollback mode. `open_db` tries on every open
until it holds.
"""
import sqlite3
import threading
import time

from orchestrator import db


def _mode(path) -> str:
    c = sqlite3.connect(str(path))
    try:
        return c.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        c.close()


def test_a_new_ledger_is_in_wal(tmp_path):
    conn = db.open_db(tmp_path / "o.db")
    db.run_migrations(conn)
    conn.close()
    assert _mode(tmp_path / "o.db") == "wal"


def test_an_existing_rollback_ledger_moves_to_wal_with_every_row(tmp_path):
    p = tmp_path / "o.db"
    c = sqlite3.connect(str(p))
    c.execute("PRAGMA journal_mode=DELETE")
    c.execute("CREATE TABLE t (x)")
    c.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(50)])
    c.commit(); c.close()
    assert _mode(p) == "delete"
    conn = db.open_db(p)
    assert conn.execute("SELECT count(*) FROM t").fetchone()[0] == 50
    conn.close()
    assert _mode(p) == "wal"


def test_a_switch_blocked_by_a_reader_does_not_fail_the_open_and_holds_on_a_later_one(tmp_path):
    p = tmp_path / "o.db"
    c = sqlite3.connect(str(p))
    c.execute("PRAGMA journal_mode=DELETE")
    c.execute("CREATE TABLE t (x)"); c.commit()
    reader = sqlite3.connect(str(p))
    reader.execute("BEGIN"); reader.execute("SELECT count(*) FROM t").fetchone()   # holds a read lock
    conn = db.open_db(p)                                                          # must not raise
    conn.close()
    reader.rollback(); reader.close(); c.close()
    db.open_db(p).close()
    assert _mode(p) == "wal"


def test_a_reader_in_an_open_transaction_does_not_block_a_writer(tmp_path):
    p = tmp_path / "o.db"
    conn = db.open_db(p)
    conn.execute("CREATE TABLE t (x)"); conn.commit()
    reader = db.open_db(p)
    reader.execute("BEGIN"); reader.execute("SELECT count(*) FROM t").fetchone()
    writer = db.open_db(p)
    writer.execute("PRAGMA busy_timeout=200")
    t0 = time.perf_counter()
    writer.execute("INSERT INTO t VALUES (1)"); writer.commit()
    assert time.perf_counter() - t0 < 0.2, "a reader held the writer up"
    reader.rollback()
    for c in (reader, writer, conn):
        c.close()


def test_the_dashboards_read_only_open_reads_a_wal_ledger_nobody_has_open(tmp_path, monkeypatch):
    p = tmp_path / "o.db"
    conn = db.open_db(p)
    db.run_migrations(conn)
    conn.execute("INSERT INTO tool_call_log (session_id, cwd, tool_name) VALUES ('s', '/x', 'Read')")
    conn.commit(); conn.close()                       # the last connection closes: -wal / -shm are gone
    ro = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    try:
        assert ro.execute("SELECT count(*) FROM tool_call_log").fetchone()[0] == 1
    finally:
        ro.close()
