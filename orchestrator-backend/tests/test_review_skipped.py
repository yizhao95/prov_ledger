"""S1 / E6-3: when review_and_complete decides NOT to review a plan it says why —
Plans.review_skip_reason + a [REVIEW SKIPPED] log line — never silently."""
import json

from orchestrator import api, db


def _registry(tmp_path, *names):
    p = tmp_path / "projects.json"
    p.write_text(json.dumps({"projects": [{"name": n, "repo": "/x", "db_path": f"/g/{n}.db", "commit_sha": "c"} for n in names]}))
    return str(p)


def _seed(conn, plan_id, goal, statuses=("COMPLETED",)):
    db.insert_plan(conn, plan_id, goal)
    for i, st in enumerate(statuses):
        db.insert_step(conn, f"{plan_id}-{chr(65 + i)}", plan_id, f"CODE: work on {goal}", i, status=st)
    return db.insert_review_step(conn, plan_id)


def test_e6_3_unregistered_plan_records_review_skipped(conn, tmp_path):
    reg = _registry(tmp_path, "other-project")
    review = _seed(conn, "P1", "tidy up the notebook")
    out = api.review_and_complete(conn, "P1", registry_path=reg)
    assert out["plan_status"] == "COMPLETED" and out["ready"] is True
    assert out["review_skipped"].startswith("no registered project mentioned")
    assert "FL-014" in out["review_skipped"]
    plan = db.get_plan(conn, "P1")
    assert plan["review_skip_reason"] == out["review_skipped"] and plan["status"] == "COMPLETED"
    assert "[REVIEW SKIPPED] no registered project" in (db.get_step(conn, review)["log_context"] or "")


def test_review_skipped_names_missing_registry(conn, tmp_path):
    _seed(conn, "P2", "anything")
    out = api.review_and_complete(conn, "P2", registry_path=str(tmp_path / "absent.json"))
    assert out["plan_status"] == "COMPLETED" and "registry not found" in out["review_skipped"]
    assert "absent.json" in db.get_plan(conn, "P2")["review_skip_reason"]


def test_review_skipped_names_empty_registry(conn, tmp_path):
    reg = tmp_path / "empty.json"; reg.write_text('{"projects": []}')
    _seed(conn, "P3", "anything")
    out = api.review_and_complete(conn, "P3", registry_path=str(reg))
    assert "registry has no projects" in out["review_skipped"]


def test_registered_plan_has_no_skip_reason(conn, tmp_path):
    reg = _registry(tmp_path, "demo-app")
    _seed(conn, "P4", "refactor the demo-app pipeline")
    out = api.review_and_complete(conn, "P4", registry_path=reg)
    assert out["needs_agent_review"] is True and "review_skipped" not in out
    assert db.get_plan(conn, "P4")["review_skip_reason"] is None


def test_failed_plan_is_not_a_skip(conn, tmp_path):
    """A FAILED close is a verdict, not a skipped review: no skip reason is written."""
    reg = _registry(tmp_path, "demo-app")
    _seed(conn, "P5", "refactor the demo-app pipeline", statuses=("FAILED",))
    out = api.review_and_complete(conn, "P5", registry_path=reg)
    assert out["plan_status"] == "FAILED" and out.get("review_skipped") is None
    assert db.get_plan(conn, "P5")["review_skip_reason"] is None


def test_detect_with_reason(conn, tmp_path):
    reg = _registry(tmp_path, "demo-app")
    _seed(conn, "P6", "unrelated")
    assert api._detect_with_reason(conn, "P6", reg) == (None, "no registered project mentioned in goal/steps (FL-014: token match)")
    _seed(conn, "P7", "touch demo app now")
    assert api._detect_with_reason(conn, "P7", reg)[0] == "demo-app"
    assert api.detect_registered_project(conn, "P7", reg) == "demo-app"
