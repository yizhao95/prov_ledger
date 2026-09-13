"""FL-022: a plan that FAILED through an unrecovered regular step (review step
FAILED with no child) is re-judged once every failure is recovered."""
import json

from orchestrator import api, db


def _registry(tmp_path, *names):
    p = tmp_path / "projects.json"
    p.write_text(json.dumps({"projects": [{"name": n, "repo": f"/repos/{n}", "db_path": str(tmp_path / f"{n}.db"), "commit_sha": "c"} for n in names]}))
    return str(p)


def _seed(conn, plan_id, goal, statuses):
    db.insert_plan(conn, plan_id, goal)
    for i, st in enumerate(statuses):
        db.insert_step(conn, f"{plan_id}-{chr(65 + i)}", plan_id, f"CODE: step {i}", i, status=st)
    return db.insert_review_step(conn, plan_id)


def _recover(conn, step_id, justification="root-caused"):
    out = api.evaluate_and_update_plan(conn, deviation_detected=True, target_step_id=step_id,
                                       justification=justification, new_sub_steps=["fix"])
    assert out.get("accepted", True), out
    api.start_step(conn, f"{step_id}.1"); api.complete_step(conn, f"{step_id}.1")


def test_fl022_plan_failed_by_regular_step_reopens_after_recovery(conn, tmp_path):
    reg = _registry(tmp_path, "prov_ledger")
    review = _seed(conn, "P1", "x", ["COMPLETED", "FAILED"])           # B failed, no children
    db.set_plan_project(conn, "P1", "prov_ledger", "declared")
    out = api.review_and_complete(conn, "P1", registry_path=reg)
    assert out["plan_status"] == "FAILED" and db.get_step(conn, review)["status"] == "FAILED"
    assert db.get_children(conn, review) == []
    _recover(conn, "P1-B")                                              # later: the agent recovers B
    out = api.review_and_complete(conn, "P1", registry_path=reg)
    assert out.get("needs_agent_review") is True and out["reopened"] is True
    assert out["project"] == "prov_ledger"
    assert db.get_plan(conn, "P1")["status"] == "IN_PROGRESS"
    assert db.get_step(conn, review)["status"] == "NEEDS_REVIEW"
    assert "[REVIEW REOPENED]" in (db.get_step(conn, review)["log_context"] or "")
    # and the normal child-driven close works from here
    db.update_step_status(conn, out["review_child_step_id"], "COMPLETED", set_completed=True)
    out = api.review_and_complete(conn, "P1", registry_path=reg)
    assert out["plan_status"] == "COMPLETED" and db.get_plan(conn, "P1")["review_state"] == "reviewed"


def test_fl022_no_reopen_while_a_failure_is_unrecovered(conn, tmp_path):
    reg = _registry(tmp_path, "prov_ledger")
    review = _seed(conn, "P1", "x", ["FAILED", "FAILED"])
    db.set_plan_project(conn, "P1", "prov_ledger", "declared")
    api.review_and_complete(conn, "P1", registry_path=reg)
    _recover(conn, "P1-A")                                              # only one of two
    out = api.review_and_complete(conn, "P1", registry_path=reg)
    assert out["plan_status"] == "FAILED" and "idempotent" in out["reason"]
    assert db.get_step(conn, review)["status"] == "FAILED"
    _recover(conn, "P1-B")
    out = api.review_and_complete(conn, "P1", registry_path=reg)
    assert out.get("needs_agent_review") is True


def test_fl022_unregistered_plan_reopens_to_completed_with_skip_reason(conn, tmp_path):
    reg = _registry(tmp_path, "other")
    review = _seed(conn, "P1", "unrelated", ["FAILED"])
    api.review_and_complete(conn, "P1", registry_path=reg)
    _recover(conn, "P1-A")
    out = api.review_and_complete(conn, "P1", registry_path=reg)
    assert out["plan_status"] == "COMPLETED" and out["reopened"] is True
    assert out["review_skipped"].startswith("no registered project")
    assert db.get_step(conn, review)["status"] == "COMPLETED"
    assert "[REVIEW REOPENED]" in (db.get_step(conn, review)["log_context"] or "")


def test_fl022_completed_plan_untouched(conn, tmp_path):
    reg = _registry(tmp_path, "other")
    _seed(conn, "P1", "unrelated", ["COMPLETED"])
    api.review_and_complete(conn, "P1", registry_path=reg)
    out = api.review_and_complete(conn, "P1", registry_path=reg)
    assert out["plan_status"] == "COMPLETED" and "idempotent" in out["reason"] and "reopened" not in out
