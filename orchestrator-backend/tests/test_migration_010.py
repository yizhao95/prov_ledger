"""Tests for migration 010 — additive data-model enrichment.

RED before 010_data_model.sql exists; GREEN after. Uses the standard `conn`
fixture (runs all migrations on a fresh in-tmpdir DB).
"""
from __future__ import annotations

import sqlite3

import pytest

from orchestrator import db


def _cols(conn, table):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def test_010_adds_step_columns(conn: sqlite3.Connection) -> None:
    assert {"failure_reason", "attempt_count", "updated_at"} <= _cols(conn, "Steps")


def test_010_adds_plan_columns(conn: sqlite3.Connection) -> None:
    assert {"updated_at", "review_state"} <= _cols(conn, "Plans")


def test_010_adds_ledger_columns(conn: sqlite3.Connection) -> None:
    assert {"superseded_by", "updated_at", "plan_id", "hit_count",
            "last_matched_at"} <= _cols(conn, "LedgerEntries")


def test_010_deviations_table(conn: sqlite3.Connection) -> None:
    assert _cols(conn, "Deviations") >= {
        "deviation_id", "plan_id", "target_step_id", "justification",
        "new_step_ids", "revision_count", "created_at"}


def test_010_idx_steps_type_restored(conn: sqlite3.Connection) -> None:
    idx = {r[1] for r in conn.execute("PRAGMA index_list(Steps)").fetchall()}
    assert "idx_steps_type" in idx


def test_010_review_state_check(conn: sqlite3.Connection) -> None:
    conn.execute("INSERT INTO Plans (plan_id, original_goal, review_state) "
                 "VALUES ('p0', 'g', 'awaiting_agent')")
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO Plans (plan_id, original_goal, review_state) "
                     "VALUES ('pbad', 'g', 'nonsense')")
        conn.commit()


def test_010_step_type_trigger_enforces_enum(conn: sqlite3.Connection) -> None:
    conn.execute("INSERT INTO Plans (plan_id, original_goal) VALUES ('p1', 'g')")
    conn.commit()
    # bad step_type rejected by the restored trigger
    with pytest.raises(sqlite3.Error):
        conn.execute(
            "INSERT INTO Steps (step_id, plan_id, description, execution_order, step_type) "
            "VALUES ('sbad', 'p1', 'd', 0, 'bogus')")
        conn.commit()
    # valid type and NULL both accepted
    conn.execute(
        "INSERT INTO Steps (step_id, plan_id, description, execution_order, step_type) "
        "VALUES ('sok', 'p1', 'd', 0, 'CODE')")
    conn.execute(
        "INSERT INTO Steps (step_id, plan_id, description, execution_order) "
        "VALUES ('snull', 'p1', 'd', 1)")
    conn.commit()


def test_010_run_migrations_idempotent(conn: sqlite3.Connection) -> None:
    assert db.run_migrations(conn) == 0
