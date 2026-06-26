"""Tests for migration 011 — DB-level COMPLETED-step immutability (BE-C5)."""
from __future__ import annotations

import sqlite3

import pytest

from orchestrator import api, db


def _completed_step(conn):
    r = api.initialize_plan(conn, "g", ["a"])
    sid = r["step_ids"][0]
    api.start_step(conn, sid)
    db.update_step_status(conn, sid, "COMPLETED", set_completed=True)
    return sid


def test_cannot_transition_out_of_completed_via_db(conn: sqlite3.Connection) -> None:
    sid = _completed_step(conn)
    with pytest.raises(sqlite3.Error):
        db.update_step_status(conn, sid, "IN_PROGRESS")
    assert db.get_step(conn, sid)["status"] == "COMPLETED"


def test_completed_step_annotations_still_allowed(conn: sqlite3.Connection) -> None:
    sid = _completed_step(conn)
    # Non-status writes on a completed step remain allowed.
    db.set_step_summary(conn, sid, "all good")
    db.set_agent_output(conn, sid, "result payload")
    step = db.get_step(conn, sid)
    assert step["summary"] == "all good"
    assert step["agent_output"] == "result payload"


def test_re_setting_completed_is_a_noop_not_an_error(conn: sqlite3.Connection) -> None:
    sid = _completed_step(conn)
    # status COMPLETED -> COMPLETED is allowed (trigger only blocks moving away).
    db.update_step_status(conn, sid, "COMPLETED", set_completed=True)
    assert db.get_step(conn, sid)["status"] == "COMPLETED"
