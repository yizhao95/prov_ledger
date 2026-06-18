"""Tests for migration 009 — adds the LedgerEntries table (provLedger Phase E).

RED before 009_ledger_entries.sql exists; GREEN after. Uses the standard `conn`
fixture (runs all migrations on a fresh in-tmpdir DB).
"""
from __future__ import annotations

import sqlite3

import pytest

from orchestrator import db


def test_ledger_entries_table_exists(conn: sqlite3.Connection) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(LedgerEntries)").fetchall()}
    expected = {"id", "project", "kind", "subjects", "keywords",
                "statement", "rationale", "status", "source", "created_at"}
    assert expected <= cols, f"LedgerEntries missing cols: {expected - cols}"


def test_kind_check_rejects_invalid(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO LedgerEntries (project, kind, statement, rationale) "
            "VALUES ('p', 'bogus', 's', 'r')")
        conn.commit()


def test_status_defaults_active(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO LedgerEntries (project, kind, statement, rationale) "
        "VALUES ('p', 'decision', 's', 'r')")
    conn.commit()
    row = conn.execute("SELECT status FROM LedgerEntries WHERE project='p'").fetchone()
    assert row["status"] == "active"


def test_run_migrations_idempotent(conn: sqlite3.Connection) -> None:
    assert db.run_migrations(conn) == 0
