"""Migration-010 column wiring (BE-D2/D3/D4, BE-C5 completed_at COALESCE)."""
from __future__ import annotations

import sqlite3

from orchestrator import api, db


def test_fail_step_persists_failure_reason(conn: sqlite3.Connection) -> None:
    r = api.initialize_plan(conn, "g", ["a"])
    sid = r["step_ids"][0]
    api.start_step(conn, sid)
    api.fail_step(conn, sid, reason="upstream table dropped a column")
    step = db.get_step(conn, sid)
    assert step["failure_reason"] == "upstream table dropped a column"


def test_status_change_sets_updated_at(conn: sqlite3.Connection) -> None:
    r = api.initialize_plan(conn, "g", ["a"])
    sid = r["step_ids"][0]
    assert db.get_step(conn, sid)["updated_at"] is None
    api.start_step(conn, sid)
    assert db.get_step(conn, sid)["updated_at"] is not None


def test_completed_at_not_restamped(conn: sqlite3.Connection) -> None:
    # BE-C5: a second set_completed must NOT overwrite the original completed_at.
    r = api.initialize_plan(conn, "g", ["a"])
    sid = r["step_ids"][0]
    api.start_step(conn, sid)
    db.update_step_status(conn, sid, "COMPLETED", set_completed=True)
    first = db.get_step(conn, sid)["completed_at"]
    db.update_step_status(conn, sid, "COMPLETED", set_completed=True)
    assert db.get_step(conn, sid)["completed_at"] == first


def test_set_review_state_roundtrip(conn: sqlite3.Connection) -> None:
    r = api.initialize_plan(conn, "g", ["a"])
    db.set_review_state(conn, r["plan_id"], "awaiting_agent")
    assert db.get_plan(conn, r["plan_id"])["review_state"] == "awaiting_agent"
    db.set_review_state(conn, r["plan_id"], "reviewed")
    assert db.get_plan(conn, r["plan_id"])["review_state"] == "reviewed"
