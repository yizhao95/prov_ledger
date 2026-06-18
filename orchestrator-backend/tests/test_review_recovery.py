"""Tests for api.review_and_complete recovery semantics.

A FAILED step is "recovered" when its deviation sub-tree resolves successfully:
  - FAILED step + all direct non-review children COMPLETED  → recovered
  - FAILED step + at least one child is itself recovered    → recovered (recursive)
  - FAILED step with no children                            → NOT recovered
  - FAILED step + any direct child is FAILED & unrecovered  → NOT recovered

A plan is FAILED only when there is at least one unrecovered FAILED step.
A plan is COMPLETED when every FAILED step is recovered (and no PENDING/IN_PROGRESS).

RED phase: these tests must fail BEFORE the recovery logic is added.
GREEN phase: they pass after _is_step_recovered is added to api.py and
review_and_complete consults it.
"""
from __future__ import annotations

import sqlite3

from orchestrator import api, db


def _seed_plan(conn: sqlite3.Connection, plan_id: str) -> None:
    db.insert_plan(conn, plan_id, f"test plan {plan_id}")


def _seed_step(
    conn: sqlite3.Connection,
    step_id: str,
    plan_id: str,
    status: str,
    execution_order: int,
    parent_step_id: str | None = None,
    depth_level: int = 0,
) -> None:
    db.insert_step(
        conn,
        step_id=step_id,
        plan_id=plan_id,
        description=f"step {step_id}",
        execution_order=execution_order,
        parent_step_id=parent_step_id,
        depth_level=depth_level,
        status=status,
    )


# ── Case 1: FAILED leaf step (no deviation) → plan FAILED ────────────────────────

def test_failed_leaf_step_not_recovered_plan_fails(conn: sqlite3.Connection) -> None:
    """A single FAILED step with no children is NOT recovered. Plan FAILS."""
    plan_id = "p-leaf-fail"
    _seed_plan(conn, plan_id)
    _seed_step(conn, f"{plan_id}-A", plan_id, "FAILED", execution_order=0)
    db.insert_review_step(conn, plan_id)

    result = api.review_and_complete(conn, plan_id)

    assert result["ready"] is True
    assert result["plan_status"] == "FAILED", (
        f"FAILED leaf with no deviation must fail the plan, got {result['plan_status']}"
    )
    assert result["review_status"] == "FAILED"


# ── Case 2: FAILED with one COMPLETED child → plan COMPLETED ──────────────────────

def test_failed_with_one_completed_child_recovered_plan_completes(conn: sqlite3.Connection) -> None:
    """FAILED parent with single COMPLETED deviation child → recovered → plan COMPLETED."""
    plan_id = "p-one-child-rec"
    _seed_plan(conn, plan_id)
    _seed_step(conn, f"{plan_id}-A", plan_id, "FAILED", execution_order=0)
    _seed_step(conn, f"{plan_id}-A.1", plan_id, "COMPLETED", execution_order=1,
               parent_step_id=f"{plan_id}-A", depth_level=1)
    db.insert_review_step(conn, plan_id)

    result = api.review_and_complete(conn, plan_id)

    assert result["ready"] is True
    assert result["plan_status"] == "COMPLETED", (
        f"FAILED parent with COMPLETED child should recover; got {result['plan_status']}"
    )
    assert result["review_status"] == "COMPLETED"


# ── Case 3: FAILED with multiple COMPLETED children → plan COMPLETED ──────────

def test_failed_with_all_completed_children_recovered(conn: sqlite3.Connection) -> None:
    """FAILED parent + 3 COMPLETED children → recovered → plan COMPLETED."""
    plan_id = "p-multi-rec"
    _seed_plan(conn, plan_id)
    _seed_step(conn, f"{plan_id}-A", plan_id, "FAILED", execution_order=0)
    for i in range(3):
        _seed_step(conn, f"{plan_id}-A.{i+1}", plan_id, "COMPLETED",
                   execution_order=i+1, parent_step_id=f"{plan_id}-A", depth_level=1)
    db.insert_review_step(conn, plan_id)

    result = api.review_and_complete(conn, plan_id)

    assert result["plan_status"] == "COMPLETED", (
        f"All 3 children COMPLETED should recover parent; got {result['plan_status']}"
    )


# ── Case 4: FAILED with one FAILED leaf child → plan FAILED ──────────────────────

def test_failed_with_one_failed_child_not_recovered(conn: sqlite3.Connection) -> None:
    """FAILED parent + COMPLETED child + FAILED leaf child → NOT recovered → plan FAILED."""
    plan_id = "p-partial-rec"
    _seed_plan(conn, plan_id)
    _seed_step(conn, f"{plan_id}-A", plan_id, "FAILED", execution_order=0)
    _seed_step(conn, f"{plan_id}-A.1", plan_id, "COMPLETED", execution_order=1,
               parent_step_id=f"{plan_id}-A", depth_level=1)
    _seed_step(conn, f"{plan_id}-A.2", plan_id, "FAILED", execution_order=2,
               parent_step_id=f"{plan_id}-A", depth_level=1)
    db.insert_review_step(conn, plan_id)

    result = api.review_and_complete(conn, plan_id)

    assert result["plan_status"] == "FAILED", (
        f"Any unrecovered FAILED child should fail the parent's recovery; got {result['plan_status']}"
    )


# ── Case 5: 3-deep recursion → plan COMPLETED ────────────────────────────────────

def test_failed_grandchild_recovers_transitively(conn: sqlite3.Connection) -> None:
    """FAILED A → FAILED A.1 → COMPLETED A.1.1.

    A.1 recovers because its only child completed; A then recovers because its
    only child (A.1) is itself recovered. Depth_level <= 3 circuit breaker is
    respected (depth 0/1/2).
    """
    plan_id = "p-deep-rec"
    _seed_plan(conn, plan_id)
    _seed_step(conn, f"{plan_id}-A", plan_id, "FAILED", execution_order=0, depth_level=0)
    _seed_step(conn, f"{plan_id}-A.1", plan_id, "FAILED", execution_order=1,
               parent_step_id=f"{plan_id}-A", depth_level=1)
    _seed_step(conn, f"{plan_id}-A.1.1", plan_id, "COMPLETED", execution_order=2,
               parent_step_id=f"{plan_id}-A.1", depth_level=2)
    db.insert_review_step(conn, plan_id)

    result = api.review_and_complete(conn, plan_id)

    assert result["plan_status"] == "COMPLETED", (
        f"Transitive recovery through grandchild should complete plan; got {result['plan_status']}"
    )


# ── Case 6: recovered plan's reason string mentions 'recovered' ──────────────────

def test_recovered_failure_reason_string_mentions_recovered(conn: sqlite3.Connection) -> None:
    """When recovery saves a plan, the human-readable reason must say so explicitly.

    This is what surfaces in the dashboard + log_context so the agent and the
    user can both see WHY the plan still completed despite the FAILED step.
    """
    plan_id = "p-reason-rec"
    _seed_plan(conn, plan_id)
    _seed_step(conn, f"{plan_id}-A", plan_id, "FAILED", execution_order=0)
    _seed_step(conn, f"{plan_id}-A.1", plan_id, "COMPLETED", execution_order=1,
               parent_step_id=f"{plan_id}-A", depth_level=1)
    db.insert_review_step(conn, plan_id)

    result = api.review_and_complete(conn, plan_id)

    assert result["plan_status"] == "COMPLETED"
    assert "recovered" in result["reason"].lower(), (
        f"reason must explain recovery; got {result['reason']!r}"
    )


# ── Case 7: the symlink-sync real-world bug scenario ────────────────────────────

def test_symlink_sync_scenario_19c_2f_recovers(conn: sqlite3.Connection) -> None:
    """Mirrors the exact shape of plan symlink-sync-20260603144713 that motivated
    this fix: 19 COMPLETED non-review steps + 2 FAILED steps each with one
    COMPLETED deviation child → plan should COMPLETED, not FAILED.
    """
    plan_id = "p-symlink-mirror"
    _seed_plan(conn, plan_id)

    # 19 COMPLETED siblings
    for i in range(19):
        _seed_step(conn, f"{plan_id}-S{i:02d}", plan_id, "COMPLETED", execution_order=i)

    # FAILED D + COMPLETED D.1
    _seed_step(conn, f"{plan_id}-D", plan_id, "FAILED", execution_order=19)
    _seed_step(conn, f"{plan_id}-D.1", plan_id, "COMPLETED", execution_order=20,
               parent_step_id=f"{plan_id}-D", depth_level=1)

    # FAILED N + COMPLETED N.1
    _seed_step(conn, f"{plan_id}-N", plan_id, "FAILED", execution_order=21)
    _seed_step(conn, f"{plan_id}-N.1", plan_id, "COMPLETED", execution_order=22,
               parent_step_id=f"{plan_id}-N", depth_level=1)

    db.insert_review_step(conn, plan_id)

    result = api.review_and_complete(conn, plan_id)

    assert result["plan_status"] == "COMPLETED", (
        f"symlink-sync shape (19c + 2f-each-recovered) must complete the plan; "
        f"got status={result['plan_status']}, reason={result['reason']!r}"
    )
    assert result["review_status"] == "COMPLETED"
