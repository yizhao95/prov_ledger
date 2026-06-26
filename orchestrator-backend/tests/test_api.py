"""Tests for api.py — initialize_plan + evaluate_and_update_plan + helpers."""
import pytest

from orchestrator import api, db
from orchestrator.circuit_breakers import HardStop
from orchestrator.state_machine import InvalidTransitionError


def test_initialize_plan_creates_plan_and_steps(conn):
    result = api.initialize_plan(conn, "test goal", ["step a", "step b", "step c"])
    assert "plan_id" in result and "step_ids" in result
    assert len(result["step_ids"]) == 3
    assert result["step_ids"][0].endswith("-A")
    assert result["step_ids"][2].endswith("-C")
    plan = db.get_plan(conn, result["plan_id"])
    assert plan["original_goal"] == "test goal"
    steps = db.get_steps(conn, result["plan_id"])
    assert [s["description"] for s in steps] == ["step a", "step b", "step c"]
    assert all(s["status"] == "PENDING" for s in steps)


def test_initialize_plan_rejects_empty_steps(conn):
    with pytest.raises(ValueError):
        api.initialize_plan(conn, "g", [])


def test_start_complete_step_happy_path(conn):
    r = api.initialize_plan(conn, "g", ["a", "b"])
    sid = r["step_ids"][0]
    s = api.start_step(conn, sid)
    assert s["status"] == "IN_PROGRESS"
    assert s["started_at"]
    s = api.complete_step(conn, sid)
    assert s["status"] == "COMPLETED"
    assert s["completed_at"]


def test_complete_step_on_completed_raises(conn):
    """Circuit breaker #1 — immutability."""
    r = api.initialize_plan(conn, "g", ["a"])
    sid = r["step_ids"][0]
    api.start_step(conn, sid)
    api.complete_step(conn, sid)
    with pytest.raises(Exception) as exc:  # SoftStop OR InvalidTransition
        api.complete_step(conn, sid)
    # accept either exception type — both are signals it's blocked
    assert "COMPLETED" in str(exc.value) or "Cannot" in str(exc.value)


def test_evaluate_no_deviation_is_noop(conn):
    r = api.initialize_plan(conn, "g", ["a"])
    out = api.evaluate_and_update_plan(conn, deviation_detected=False)
    assert out["accepted"] is True and out["no_changes"] is True
    assert db.get_plan(conn, r["plan_id"])["revision_count"] == 0


def test_evaluate_with_deviation_inserts_substeps(conn):
    r = api.initialize_plan(conn, "g", ["a"])
    sid = r["step_ids"][0]
    out = api.evaluate_and_update_plan(
        conn,
        deviation_detected=True,
        target_step_id=sid,
        justification="discovered API needs auth refresh",
        new_sub_steps=["refresh token", "retry"],
    )
    assert out["accepted"] is True
    assert len(out["new_step_ids"]) == 2
    assert out["revision_count"] == 1
    children = db.get_children(conn, sid)
    assert len(children) == 2
    assert all(c["depth_level"] == 1 for c in children)
    assert all(c["parent_step_id"] == sid for c in children)


def test_evaluate_persists_deviation_justification(conn):
    # BE-C1: the justification must survive in a queryable Deviations row.
    r = api.initialize_plan(conn, "g", ["a"])
    sid = r["step_ids"][0]
    out = api.evaluate_and_update_plan(
        conn,
        deviation_detected=True,
        target_step_id=sid,
        justification="rolling-window split, not random — avoids temporal leakage",
        new_sub_steps=["use TimeSeriesSplit"],
    )
    devs = db.get_deviations(conn, r["plan_id"])
    assert len(devs) == 1
    assert devs[0]["justification"] == "rolling-window split, not random — avoids temporal leakage"
    assert devs[0]["target_step_id"] == sid
    assert devs[0]["revision_count"] == 1
    assert out["new_step_ids"][0] in devs[0]["new_step_ids"]  # JSON list contains the sub-step
    assert out["deviation_id"] == devs[0]["deviation_id"]


def test_evaluate_blocks_substeps_into_completed_step(conn):
    """Cannot insert children under a COMPLETED parent (circuit breaker #1)."""
    r = api.initialize_plan(conn, "g", ["a"])
    sid = r["step_ids"][0]
    api.start_step(conn, sid)
    api.complete_step(conn, sid)
    out = api.evaluate_and_update_plan(
        conn,
        deviation_detected=True,
        target_step_id=sid,
        justification="too late",
        new_sub_steps=["nope"],
    )
    assert out["accepted"] is False
    assert "COMPLETED" in out["reason"]


def test_evaluate_hardstop_at_max_revisions(conn):
    r = api.initialize_plan(conn, "g", ["a"], max_revisions=2)
    sid = r["step_ids"][0]
    # First two revisions allowed
    for i in range(2):
        api.evaluate_and_update_plan(
            conn, deviation_detected=True, target_step_id=sid,
            justification=f"rev {i}", new_sub_steps=None,  # no new substeps to keep depth=0
        )
    # Third should HardStop
    with pytest.raises(HardStop):
        api.evaluate_and_update_plan(
            conn, deviation_detected=True, target_step_id=sid, justification="too many",
        )


def test_evaluate_blocks_depth_4(conn):
    """SoftStop when trying to nest beyond depth 3."""
    r = api.initialize_plan(conn, "g", ["a"])
    sid = r["step_ids"][0]
    # Manually create a depth-3 step
    db.insert_step(conn, "deep-3", r["plan_id"], "depth 3", execution_order=99,
                   parent_step_id=sid, depth_level=3)
    out = api.evaluate_and_update_plan(
        conn, deviation_detected=True, target_step_id="deep-3",
        justification="try to go to depth 4", new_sub_steps=["nope"],
    )
    assert out["accepted"] is False
    assert "depth" in out["reason"].lower()


def test_fail_step_marks_failed(conn):
    r = api.initialize_plan(conn, "g", ["a"])
    sid = r["step_ids"][0]
    api.start_step(conn, sid)
    s = api.fail_step(conn, sid, reason="network error")
    assert s["status"] == "FAILED"
    assert s["completed_at"]
    assert "network error" in s["log_context"]


def test_failed_can_resume_to_in_progress(conn):
    """User-unblock: FAILED → IN_PROGRESS allowed."""
    r = api.initialize_plan(conn, "g", ["a"])
    sid = r["step_ids"][0]
    api.start_step(conn, sid)
    api.fail_step(conn, sid, reason="boom")
    # Re-start should be allowed via direct status update
    db.update_step_status(conn, sid, "IN_PROGRESS")
    s = db.get_step(conn, sid)
    assert s["status"] == "IN_PROGRESS"


def test_invalid_transition_pending_to_completed_via_helper(conn):
    """start_step → complete_step required; cannot skip start."""
    r = api.initialize_plan(conn, "g", ["a"])
    sid = r["step_ids"][0]
    with pytest.raises(InvalidTransitionError):
        api.complete_step(conn, sid)  # still PENDING
