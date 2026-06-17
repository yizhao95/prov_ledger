"""End-to-end tests for the auto-review-and-complete deterministic flow.

Six scenarios per spec:
  (a) publish-plan auto-appends a review step (is_review=1, last in order, PENDING)
  (b) completing the LAST regular step auto-completes the whole plan
  (c) failing a step (with all others COMPLETED) auto-fails the plan
  (d) leaving a step PENDING blocks the auto-review (no-op)
  (e) the review procedure is idempotent — re-running on a terminal plan is safe
  (f) legacy plans (no review row) still work via finish-plan.sh

Uses the seeded_plan fixture + run_script_fn from executing-plans/tests/conftest.py
to drive the REAL scripts against an ephemeral DB.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

ORCH_ROOT = Path.home() / "skill-workspace" / "orchestrator"
sys.path.insert(0, str(ORCH_ROOT))

from orchestrator import db as orch_db  # noqa: E402


# ── (a) publish-plan must auto-append a review step ─────────────────────────────

def test_publish_plan_appends_review_step(seeded_plan, tmp_db):
    """After publish-plan.sh runs (via seeded_plan fixture), Steps table has
    N+1 rows: the N user-defined steps plus exactly one is_review=1 row
    named '<plan>-REVIEW' at execution_order = max+1.
    """
    plan_id = seeded_plan["plan_id"]
    conn = sqlite3.connect(str(tmp_db))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT step_id, status, is_review, execution_order FROM Steps "
        "WHERE plan_id = ? ORDER BY execution_order",
        (plan_id,),
    ).fetchall()
    conn.close()

    # 3 user steps + 1 review step = 4
    assert len(rows) == 4, f"expected 4 steps, got {len(rows)}: {[dict(r) for r in rows]}"
    # The first 3 are non-review
    assert all(r["is_review"] == 0 for r in rows[:3])
    # The last is the review step
    last = rows[-1]
    assert last["is_review"] == 1, f"last step must be the review marker; got {dict(last)}"
    assert last["step_id"] == f"{plan_id}-REVIEW"
    assert last["status"] == "PENDING"


# ── (b) completing the last regular step auto-completes the plan ─────────────────

def test_complete_last_step_auto_completes_plan(seeded_plan, tmp_db, run_script_fn):
    """Walk the 3 seed steps start→complete. After the LAST one completes,
    the auto-trigger fires review_and_complete which marks plan COMPLETED."""
    plan_id = seeded_plan["plan_id"]
    step_ids = seeded_plan["step_ids"]  # 3 user-defined IDs

    for sid in step_ids:
        run_script_fn("start-step", {"step_id": sid, "type": "CODE"}, tmp_db)
        run_script_fn("complete-step", {"step_id": sid, "summary": f"{sid} ok"}, tmp_db)

    conn = sqlite3.connect(str(tmp_db))
    conn.row_factory = sqlite3.Row
    plan_status = conn.execute(
        "SELECT status, completed_at FROM Plans WHERE plan_id = ?", (plan_id,)
    ).fetchone()
    review_status = conn.execute(
        "SELECT status FROM Steps WHERE plan_id = ? AND is_review = 1", (plan_id,)
    ).fetchone()
    conn.close()

    assert plan_status["status"] == "COMPLETED", (
        f"plan must auto-promote to COMPLETED; got {plan_status['status']}. "
        "Did the auto-trigger fire from _op_complete_step?"
    )
    assert plan_status["completed_at"] is not None
    assert review_status["status"] == "COMPLETED", (
        f"review step must also be COMPLETED; got {review_status['status']}"
    )


# ── (c) failing a step (rest COMPLETED) auto-fails the plan ─────────────────────

def test_fail_step_auto_fails_plan(seeded_plan, tmp_db, run_script_fn):
    """Complete first 2 steps, FAIL the 3rd. Review trigger sees one FAILED
    + rest terminal → marks review FAILED + plan FAILED."""
    plan_id = seeded_plan["plan_id"]
    sa, sb, sc = seeded_plan["step_ids"]

    run_script_fn("start-step", {"step_id": sa, "type": "CODE"}, tmp_db)
    run_script_fn("complete-step", {"step_id": sa, "summary": "ok"}, tmp_db)
    run_script_fn("start-step", {"step_id": sb, "type": "CODE"}, tmp_db)
    run_script_fn("complete-step", {"step_id": sb, "summary": "ok"}, tmp_db)
    run_script_fn("start-step", {"step_id": sc, "type": "CODE"}, tmp_db)
    run_script_fn("fail-step", {"step_id": sc, "reason": "boom"}, tmp_db)

    conn = sqlite3.connect(str(tmp_db))
    conn.row_factory = sqlite3.Row
    plan_row = conn.execute("SELECT status FROM Plans WHERE plan_id = ?", (plan_id,)).fetchone()
    review_row = conn.execute(
        "SELECT status FROM Steps WHERE plan_id = ? AND is_review = 1", (plan_id,)
    ).fetchone()
    conn.close()

    assert plan_row["status"] == "FAILED", f"plan must auto-FAIL; got {plan_row['status']}"
    assert review_row["status"] == "FAILED", f"review must also be FAILED; got {review_row['status']}"


# ── (d) leaving a step PENDING blocks the auto-review ───────────────────────────

def test_pending_step_blocks_auto_review(seeded_plan, tmp_db, run_script_fn):
    """Complete only 2 of 3 steps. Plan must stay IN_PROGRESS, review PENDING."""
    plan_id = seeded_plan["plan_id"]
    sa, sb, sc = seeded_plan["step_ids"]

    run_script_fn("start-step", {"step_id": sa, "type": "CODE"}, tmp_db)
    run_script_fn("complete-step", {"step_id": sa, "summary": "ok"}, tmp_db)
    run_script_fn("start-step", {"step_id": sb, "type": "CODE"}, tmp_db)
    run_script_fn("complete-step", {"step_id": sb, "summary": "ok"}, tmp_db)
    # sc left PENDING intentionally

    conn = sqlite3.connect(str(tmp_db))
    conn.row_factory = sqlite3.Row
    plan_row = conn.execute("SELECT status FROM Plans WHERE plan_id = ?", (plan_id,)).fetchone()
    review_row = conn.execute(
        "SELECT status FROM Steps WHERE plan_id = ? AND is_review = 1", (plan_id,)
    ).fetchone()
    conn.close()

    assert plan_row["status"] == "IN_PROGRESS", (
        f"plan must NOT auto-promote while a step is PENDING; got {plan_row['status']}"
    )
    assert review_row["status"] == "PENDING", (
        f"review must stay PENDING; got {review_row['status']}"
    )


# ── (e) review_and_complete is idempotent on terminal plans ──────────────────────

def test_review_and_complete_is_idempotent(seeded_plan, tmp_db, run_script_fn):
    """After plan reaches COMPLETED, calling finish-plan.sh again must be a no-op
    (not error, not change completed_at)."""
    plan_id = seeded_plan["plan_id"]
    for sid in seeded_plan["step_ids"]:
        run_script_fn("start-step", {"step_id": sid, "type": "CODE"}, tmp_db)
        run_script_fn("complete-step", {"step_id": sid, "summary": "ok"}, tmp_db)

    conn = sqlite3.connect(str(tmp_db))
    conn.row_factory = sqlite3.Row
    completed_at_before = conn.execute(
        "SELECT completed_at FROM Plans WHERE plan_id = ?", (plan_id,)
    ).fetchone()["completed_at"]
    conn.close()
    assert completed_at_before is not None

    # Manual re-invocation of finish-plan.sh — must succeed silently
    result = run_script_fn("finish-plan", {"plan_id": plan_id}, tmp_db)
    assert result.returncode == 0, f"finish-plan must be idempotent: stderr={result.stderr!r}"

    conn = sqlite3.connect(str(tmp_db))
    conn.row_factory = sqlite3.Row
    after = conn.execute(
        "SELECT status, completed_at FROM Plans WHERE plan_id = ?", (plan_id,)
    ).fetchone()
    conn.close()
    assert after["status"] == "COMPLETED"
    assert after["completed_at"] == completed_at_before, (
        f"completed_at must not change on idempotent re-call: "
        f"before={completed_at_before!r}, after={after['completed_at']!r}"
    )


# ── (f) legacy plans (no review row) still work via finish-plan.sh ──────────────

def test_legacy_plan_finish_still_works(tmp_db, run_script_fn):
    """A plan inserted WITHOUT calling insert_review_step (simulating a
    pre-2026-05-26 plan) must still be finishable by finish-plan.sh. The
    unified procedure falls back to plain complete_plan when no review row exists."""
    # Insert a legacy-style plan directly (bypass publish-plan.sh)
    conn = sqlite3.connect(str(tmp_db))
    orch_db.insert_plan(conn, "legacy-plan-1", "pre-review-step plan")
    orch_db.insert_step(conn, "legacy-plan-1-A", "legacy-plan-1", "dummy", execution_order=0)
    conn.close()

    # Complete the regular step (no auto-trigger because there's no review row to flip)
    run_script_fn("start-step", {"step_id": "legacy-plan-1-A", "type": "CODE"}, tmp_db)
    run_script_fn("complete-step", {"step_id": "legacy-plan-1-A", "summary": "ok"}, tmp_db)

    # Plan still IN_PROGRESS — proves no review row was found
    conn = sqlite3.connect(str(tmp_db))
    conn.row_factory = sqlite3.Row
    status_before = conn.execute(
        "SELECT status FROM Plans WHERE plan_id = ?", ("legacy-plan-1",)
    ).fetchone()["status"]
    conn.close()
    assert status_before == "IN_PROGRESS", (
        f"Legacy plan must stay IN_PROGRESS (no auto-trigger without review row); "
        f"got {status_before}"
    )

    # Now call finish-plan.sh — must fall back to plain complete_plan
    result = run_script_fn("finish-plan", {"plan_id": "legacy-plan-1"}, tmp_db)
    assert result.returncode == 0, f"finish-plan failed: {result.stderr!r}"

    conn = sqlite3.connect(str(tmp_db))
    conn.row_factory = sqlite3.Row
    status_after = conn.execute(
        "SELECT status, completed_at FROM Plans WHERE plan_id = ?", ("legacy-plan-1",)
    ).fetchone()
    conn.close()
    assert status_after["status"] == "COMPLETED", (
        f"legacy fallback must mark plan COMPLETED; got {status_after['status']}"
    )
    assert status_after["completed_at"] is not None
