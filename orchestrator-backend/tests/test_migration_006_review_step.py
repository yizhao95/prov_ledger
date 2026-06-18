"""Tests for migration 006 — adds is_review column + index to Steps table.

RED phase: these tests must fail BEFORE migration 006 exists.
GREEN phase: they pass after 006_review_step.sql is added to migrations/.

Tests use the standard `conn` fixture from conftest.py which runs all
migrations on a fresh in-tmpdir DB.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from orchestrator import db


def test_is_review_column_exists_with_default_0(conn: sqlite3.Connection) -> None:
    """Steps.is_review column added by migration 006 with NOT NULL DEFAULT 0."""
    cols = {row[1]: row for row in conn.execute("PRAGMA table_info(Steps)").fetchall()}
    assert "is_review" in cols, (
        f"is_review column missing from Steps table. Existing cols: {sorted(cols.keys())}"
    )
    # PRAGMA table_info row shape: (cid, name, type, notnull, dflt_value, pk)
    col = cols["is_review"]
    assert col[3] == 1, f"is_review must be NOT NULL, got notnull={col[3]}"
    assert str(col[4]) == "0", f"is_review default must be 0, got {col[4]!r}"

    # And: inserting a step without specifying is_review yields 0
    db.insert_plan(conn, "p-default-check", "test default value")
    db.insert_step(conn, "p-default-check-A", "p-default-check", "dummy", execution_order=0)
    row = conn.execute(
        "SELECT is_review FROM Steps WHERE step_id = ?", ("p-default-check-A",)
    ).fetchone()
    assert row["is_review"] == 0, f"new step without is_review must default to 0, got {row['is_review']}"


def test_idx_steps_plan_review_index_exists(conn: sqlite3.Connection) -> None:
    """Migration 006 creates idx_steps_plan_review on Steps(plan_id, is_review)."""
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_steps_plan_review'"
    ).fetchone()
    assert row is not None, "idx_steps_plan_review index missing"


def test_migration_006_is_idempotent(tmp_path: Path) -> None:
    """Running run_migrations twice on the same DB must not error.

    Important because users may re-run migrations during dev. Migration 006
    must use IF NOT EXISTS for the index and the schema_version tracking
    must skip already-applied 006_review_step.sql.
    """
    db_path = tmp_path / "idem.db"
    c1 = db.open_db(db_path)
    applied_first = db.run_migrations(c1)
    c1.close()

    c2 = db.open_db(db_path)
    applied_second = db.run_migrations(c2)
    # Second run should apply 0 migrations (schema_version tracking blocks re-runs)
    assert applied_second == 0, (
        f"Second run_migrations applied {applied_second} files; expected 0 (all already applied)"
    )
    # Column still exists, index still exists
    cols = {row[1] for row in c2.execute("PRAGMA table_info(Steps)").fetchall()}
    assert "is_review" in cols
    idx = c2.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_steps_plan_review'"
    ).fetchone()
    assert idx is not None
    c2.close()


# ── db.insert_review_step helper ─────────────────────────────────────────────

def test_insert_review_step_creates_marker_row(conn: sqlite3.Connection) -> None:
    """db.insert_review_step(conn, plan_id) creates the canonical review-marker row.

    Required shape:
      - step_id = f"{plan_id}-REVIEW"
      - is_review = 1
      - status = 'PENDING' (will be flipped by api.review_and_complete)
      - depth_level = 0, parent_step_id = NULL
      - execution_order = max(existing) + 1 (sorts AFTER all regular steps)
      - description names itself clearly so the dashboard renders it sensibly
    """
    db.insert_plan(conn, "p-rev-1", "plan with review step")
    db.insert_step(conn, "p-rev-1-A", "p-rev-1", "first real step", execution_order=0)
    db.insert_step(conn, "p-rev-1-B", "p-rev-1", "second real step", execution_order=1)

    new_sid = db.insert_review_step(conn, "p-rev-1")

    assert new_sid == "p-rev-1-REVIEW", f"step_id must be '<plan>-REVIEW', got {new_sid!r}"
    row = conn.execute("SELECT * FROM Steps WHERE step_id = ?", (new_sid,)).fetchone()
    assert row is not None, "review step not inserted"
    assert row["is_review"] == 1, f"is_review must be 1, got {row['is_review']}"
    assert row["status"] == "PENDING", f"status must be PENDING, got {row['status']!r}"
    assert row["depth_level"] == 0
    assert row["parent_step_id"] is None
    assert row["execution_order"] == 2, (
        f"execution_order must be max(0,1)+1=2, got {row['execution_order']}"
    )
    assert "review" in row["description"].lower(), (
        f"description must self-describe; got {row['description']!r}"
    )


def test_insert_review_step_first_step_when_plan_empty(conn: sqlite3.Connection) -> None:
    """Edge case: plan with zero existing steps → review step gets execution_order=0.

    Not the normal flow (publish-plan rejects empty steps lists), but the helper
    must not crash if called against an empty plan.
    """
    db.insert_plan(conn, "p-rev-empty", "plan with no real steps")
    new_sid = db.insert_review_step(conn, "p-rev-empty")
    row = conn.execute("SELECT * FROM Steps WHERE step_id = ?", (new_sid,)).fetchone()
    assert row["execution_order"] == 0
