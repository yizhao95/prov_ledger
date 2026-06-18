"""Tests for migration 008 — adds nullable impact_context column to Plans.

RED before 008_impact_context.sql exists; GREEN after. Uses the standard `conn`
fixture (runs all migrations on a fresh in-tmpdir DB).
"""
from __future__ import annotations

import json
import sqlite3

from orchestrator import db


def test_impact_context_column_exists(conn: sqlite3.Connection) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(Plans)").fetchall()}
    assert "impact_context" in cols, (
        f"impact_context column missing from Plans. Existing: {sorted(cols)}")


def test_impact_context_nullable_default(conn: sqlite3.Connection) -> None:
    # a plan inserted without impact_context has NULL there (backward compatible)
    db.insert_plan(conn, "p-ic-null", "no impact context")
    row = conn.execute(
        "SELECT impact_context FROM Plans WHERE plan_id=?", ("p-ic-null",)).fetchone()
    assert row["impact_context"] is None


def test_set_plan_impact_context_round_trip(conn: sqlite3.Connection) -> None:
    db.insert_plan(conn, "p-ic", "with impact context")
    payload = {"symbols": [{"name": "x", "status": "existing"}], "ledger_reminders": []}
    db.set_plan_impact_context(conn, "p-ic", json.dumps(payload))
    row = conn.execute(
        "SELECT impact_context FROM Plans WHERE plan_id=?", ("p-ic",)).fetchone()
    assert json.loads(row["impact_context"]) == payload


def test_run_migrations_idempotent(conn: sqlite3.Connection) -> None:
    # second run must be a clean no-op (no duplicate-column explosion)
    applied = db.run_migrations(conn)
    assert applied == 0
