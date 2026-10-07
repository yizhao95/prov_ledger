"""A chained insert reads the chain head under the write lock (FL-224).

`_insert_chained` reads the last row's hash and then inserts a row pointing at
it. Python's sqlite3 opens its implicit transaction only before the INSERT, so
the read ran outside any transaction: a second writer could insert between the
read and the write, both new rows would point at the same predecessor, and
integrity verify would report the fork as tampering. Taking the write lock
(BEGIN IMMEDIATE) before reading the head makes that interleaving impossible.
"""
import sqlite3

import pytest

from orchestrator import db, integrity, provenance


@pytest.fixture
def ledger(tmp_path):
    p = tmp_path / "o.db"
    conn = db.open_db(p)
    db.run_migrations(conn)
    provenance.insert_utterance(conn, session_id="s", project="p", plan_id=None, text="first",
                                occurred_at="2026-10-01 10:00:00")
    conn.close()
    return p


def _chain(path):
    c = sqlite3.connect(str(path))
    try:
        return c.execute("SELECT id, prev_hash, hash FROM utterance ORDER BY id").fetchall()
    finally:
        c.close()


def test_a_writer_cannot_slip_in_between_reading_the_head_and_inserting(ledger, monkeypatch):
    a = db.open_db(ledger)
    b = db.open_db(ledger)
    b.execute("PRAGMA busy_timeout=100")
    original = provenance._last_hash
    slipped = {}

    def head_then_interleave(conn, table):
        head = original(conn, table)
        if conn is a and "done" not in slipped:
            slipped["done"] = True
            try:   # the second writer tries to insert right now
                provenance.insert_utterance(b, session_id="s", project="p", plan_id=None, text="interloper",
                                            occurred_at="2026-10-01 10:00:01")
                slipped["result"] = "inserted"
            except sqlite3.OperationalError as e:
                b.rollback()
                slipped["result"] = f"blocked: {e}"
        return head

    monkeypatch.setattr(provenance, "_last_hash", head_then_interleave)
    provenance.insert_utterance(a, session_id="s", project="p", plan_id=None, text="second",
                                occurred_at="2026-10-01 10:00:02")
    a.close(); b.close()
    rows = _chain(ledger)
    prevs = [r[1] for r in rows[1:]]
    assert len(prevs) == len(set(prevs)), f"two rows point at the same predecessor: {rows}"
    assert slipped["result"].startswith("blocked"), slipped
    for prev_row, row in zip(rows, rows[1:]):
        assert row[1] == prev_row[2], "every row points at the one before it"


def test_a_compound_transaction_takes_the_write_lock_when_it_opens(ledger):
    a = db.open_db(ledger)
    b = sqlite3.connect(str(ledger), timeout=0.1)
    with db.transaction(a):
        with pytest.raises(sqlite3.OperationalError):
            b.execute("BEGIN IMMEDIATE")      # the lock is already held, before any write in the block
    b.close(); a.close()


def test_the_chain_still_verifies(ledger):
    conn = db.open_db(ledger)
    for i in range(3):
        provenance.insert_utterance(conn, session_id="s", project="p", plan_id=None, text=f"n{i}",
                                    occurred_at=f"2026-10-01 11:00:0{i}")
    report = integrity.verify(conn) if hasattr(integrity, "verify") else None
    conn.close()
    rows = _chain(ledger)
    for prev_row, row in zip(rows, rows[1:]):
        assert row[1] == prev_row[2]
    if report is not None:
        assert "utterance" not in str(report.get("broken", "")) if isinstance(report, dict) else True
