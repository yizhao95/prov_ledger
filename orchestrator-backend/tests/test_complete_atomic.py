"""FL-017: status + log land together or not at all (commit= on the api layer)."""
import pytest

from orchestrator import api, db


def _started(conn):
    r = api.initialize_plan(conn, "g", ["a", "b"])
    sid = r["step_ids"][0]
    api.start_step(conn, sid)
    return sid


def test_append_log_can_join_a_transaction(conn):
    sid = _started(conn)
    with pytest.raises(RuntimeError):
        with db.transaction(conn):
            api.complete_step(conn, sid, commit=False)
            api.append_log(conn, sid, "partial", commit=False)
            raise RuntimeError("boom")
    step = db.get_step(conn, sid)
    assert step["status"] == "IN_PROGRESS" and "partial" not in (step["log_context"] or "")   # nothing half-written


def test_fail_step_can_join_a_transaction(conn):
    sid = _started(conn)
    with pytest.raises(RuntimeError):
        with db.transaction(conn):
            api.fail_step(conn, sid, "disk full", commit=False)
            raise RuntimeError("boom")
    step = db.get_step(conn, sid)
    assert step["status"] == "IN_PROGRESS" and step["failure_reason"] is None
    assert "[FAILED]" not in (step["log_context"] or "")


def test_default_commit_behaviour_unchanged(conn):
    sid = _started(conn)
    api.append_log(conn, sid, "hello")
    api.complete_step(conn, sid)
    step = db.get_step(conn, sid)
    assert step["status"] == "COMPLETED" and "hello" in step["log_context"]
