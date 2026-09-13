"""FL-014: which project a plan belongs to is read from Plans.project — declared
or derived at publish — not guessed from goal text. Token matching survives
only for NULL (legacy) plans and is labelled as such."""
import json

from orchestrator import api, db


def _registry(tmp_path, *names):
    p = tmp_path / "projects.json"
    p.write_text(json.dumps({"projects": [{"name": n, "repo": f"/repos/{n}", "db_path": str(tmp_path / f"{n}.db"), "commit_sha": "c"} for n in names]}))
    return str(p)


def _seed(conn, plan_id, goal, statuses=("COMPLETED",)):
    db.insert_plan(conn, plan_id, goal)
    for i, st in enumerate(statuses):
        db.insert_step(conn, f"{plan_id}-{chr(65 + i)}", plan_id, f"CODE: step {i}", i, status=st)
    return db.insert_review_step(conn, plan_id)


def test_declared_project_reviews_without_name_in_goal(conn, tmp_path):
    reg = _registry(tmp_path, "prov_ledger")
    _seed(conn, "P1", "tidy the notebook")                             # goal never says prov_ledger
    db.set_plan_project(conn, "P1", "prov_ledger", "declared")
    out = api.review_and_complete(conn, "P1", registry_path=reg)
    assert out.get("needs_agent_review") is True and out["project"] == "prov_ledger"
    assert out["project_source"] == "declared"


def test_cwd_project_reviews_without_name_in_goal(conn, tmp_path):
    reg = _registry(tmp_path, "prov_ledger")
    db.insert_plan(conn, "P1", "tidy the notebook", project="prov_ledger", project_source="cwd")
    db.insert_step(conn, "P1-A", "P1", "CODE: step", 0, status="COMPLETED")
    db.insert_review_step(conn, "P1")
    out = api.review_and_complete(conn, "P1", registry_path=reg)
    assert out.get("needs_agent_review") is True and out["project_source"] == "cwd"


def test_project_none_skips_with_reason(conn, tmp_path):
    reg = _registry(tmp_path, "prov_ledger")
    review = _seed(conn, "P1", "work on prov_ledger")                  # goal DOES say it — irrelevant now
    db.set_plan_project(conn, "P1", "none", "declared")
    out = api.review_and_complete(conn, "P1", registry_path=reg)
    assert out["plan_status"] == "COMPLETED" and out["review_skipped"] == "plan declared project=none"
    assert "[REVIEW SKIPPED] plan declared project=none" in (db.get_step(conn, review)["log_context"] or "")


def test_unregistered_declared_project_skips_with_reason(conn, tmp_path):
    reg = _registry(tmp_path, "prov_ledger")
    _seed(conn, "P1", "work on prov_ledger")
    db.set_plan_project(conn, "P1", "ghost", "declared")
    out = api.review_and_complete(conn, "P1", registry_path=reg)
    assert out["plan_status"] == "COMPLETED" and out["review_skipped"] == "project 'ghost' not registered"


def test_legacy_null_project_falls_back_to_token_match_and_is_labelled(conn, tmp_path):
    reg = _registry(tmp_path, "prov_ledger")
    _seed(conn, "P1", "work on prov_ledger")
    out = api.review_and_complete(conn, "P1", registry_path=reg)
    assert out["project"] == "prov_ledger" and out["project_source"] == "legacy"
    p = db.get_plan(conn, "P1")
    assert (p["project"], p["project_source"]) == ("prov_ledger", "legacy")
    # a NULL plan that mentions nothing stays NULL and skips with the FL-014 reason
    _seed(conn, "P2", "unrelated work")
    out = api.review_and_complete(conn, "P2", registry_path=reg)
    assert out["review_skipped"].startswith("no registered project mentioned") and db.get_plan(conn, "P2")["project"] is None


def test_project_for_review_shape(conn, tmp_path):
    reg = _registry(tmp_path, "prov_ledger")
    _seed(conn, "P1", "x"); db.set_plan_project(conn, "P1", "prov_ledger", "cwd")
    assert api._project_for_review(conn, "P1", reg) == ("prov_ledger", "cwd", "cwd")
    _seed(conn, "P2", "x"); db.set_plan_project(conn, "P2", "none", "declared")
    assert api._project_for_review(conn, "P2", reg) == (None, "plan declared project=none", "declared")
    _seed(conn, "P3", "x"); db.set_plan_project(conn, "P3", "ghost", "declared")
    assert api._project_for_review(conn, "P3", reg) == (None, "project 'ghost' not registered", "declared")
    _seed(conn, "P4", "refactor prov_ledger")
    assert api._project_for_review(conn, "P4", reg) == ("prov_ledger", "legacy token match", "legacy")
    assert api._project_for_review(conn, "P4", reg)[1] == "legacy"           # second call: stored, no re-match
    _seed(conn, "P5", "nothing here")
    assert api._project_for_review(conn, "P5", reg) == (None, api.NO_PROJECT_MENTIONED, None)


def test_initialize_plan_records_attribution(conn):
    r = api.initialize_plan(conn, "g", ["a"], plan_id_prefix="one", project="prov_ledger", project_source="cwd")
    p = db.get_plan(conn, r["plan_id"])
    assert (p["project"], p["project_source"]) == ("prov_ledger", "cwd")
    r2 = api.initialize_plan(conn, "g", ["a"], plan_id_prefix="two")
    assert db.get_plan(conn, r2["plan_id"])["project"] is None
