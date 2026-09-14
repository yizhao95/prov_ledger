"""FL-019: a review that FAILED is not a dead end — when its deviation sub-tree
recovers, the plan can still close COMPLETED and reviewed."""
import json

from orchestrator import api, db


def _registry(tmp_path):
    p = tmp_path / "projects.json"
    p.write_text(json.dumps({"projects": [{"name": "demo-app", "repo": "/x", "db_path": str(tmp_path / "absent.db"), "commit_sha": "c"}]}))
    return str(p)


def _parked(conn, reg, plan_id="P1"):
    db.insert_plan(conn, plan_id, "refactor the demo-app pipeline")
    db.insert_step(conn, f"{plan_id}-A", plan_id, "CODE: work", 0, status="COMPLETED")
    review = db.insert_review_step(conn, plan_id)
    out = api.review_and_complete(conn, plan_id, registry_path=reg)
    child = out["review_child_step_id"]
    db.update_step_status(conn, child, "IN_PROGRESS", set_started=True)
    return review, child


def test_fl019_failed_review_reopens_after_recovery(conn, tmp_path):
    reg = _registry(tmp_path)
    review, child = _parked(conn, reg)
    api.fail_step(conn, child, "signature gate false positive")
    out = api.review_and_complete(conn, "P1", registry_path=reg)
    assert out["plan_status"] == "FAILED" and db.get_step(conn, review)["status"] == "FAILED"
    # the agent judges, fixes and records it under the FAILED child
    dev = api.evaluate_and_update_plan(conn, deviation_detected=True, target_step_id=child,
                                       justification="additive kwargs only; suite green", new_sub_steps=["manual verdict PASS + refresh"])
    assert dev.get("accepted", True), dev
    db.update_step_status(conn, f"{child}.1", "COMPLETED", set_completed=True)
    out = api.review_and_complete(conn, "P1", registry_path=reg)
    assert out["ready"] is True and out["plan_status"] == "COMPLETED"
    assert out["reopened"] is True
    assert db.get_step(conn, review)["status"] == "COMPLETED"
    plan = db.get_plan(conn, "P1")
    assert plan["status"] == "COMPLETED" and plan["review_state"] == "reviewed"
    assert "[REVIEW REOPENED]" in (db.get_step(conn, review)["log_context"] or "")


def test_fl019_unrecovered_failed_review_stays_failed(conn, tmp_path):
    reg = _registry(tmp_path)
    review, child = _parked(conn, reg)
    api.fail_step(conn, child, "real gap")
    api.review_and_complete(conn, "P1", registry_path=reg)
    api.evaluate_and_update_plan(conn, deviation_detected=True, target_step_id=child,
                                 justification="trying", new_sub_steps=["retry"])          # sub-step still PENDING
    out = api.review_and_complete(conn, "P1", registry_path=reg)
    assert out["plan_status"] == "FAILED" and db.get_plan(conn, "P1")["status"] == "FAILED"
    db.update_step_status(conn, f"{child}.1", "IN_PROGRESS", set_started=True)
    api.fail_step(conn, f"{child}.1", "still broken")
    out = api.review_and_complete(conn, "P1", registry_path=reg)
    assert out["plan_status"] == "FAILED"


def test_fl019_completed_review_is_still_idempotent(conn, tmp_path):
    reg = _registry(tmp_path)
    review, child = _parked(conn, reg)
    db.update_step_status(conn, child, "COMPLETED", set_completed=True)
    api.review_and_complete(conn, "P1", registry_path=reg)
    out = api.review_and_complete(conn, "P1", registry_path=reg)
    assert out["plan_status"] == "COMPLETED" and "idempotent" in out["reason"]


# ── FL-030: the reopened close checks the registry sha and logs the reason slots ──

def _git_repo(tmp_path):
    import subprocess
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / "m.py").write_text("x = 1\n")
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "base"], cwd=repo, check=True)
    return repo, subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True).stdout.strip()


def _registry_for(tmp_path, repo, sha):
    p = tmp_path / "projects.json"
    p.write_text(json.dumps({"projects": [{"name": "demo-app", "repo": str(repo), "db_path": str(tmp_path / "absent.db"), "commit_sha": sha}]}))
    return str(p)


def _recovered(conn, reg):
    review, child = _parked(conn, reg)
    api.fail_step(conn, child, "gates failed")
    api.review_and_complete(conn, "P1", registry_path=reg)
    api.evaluate_and_update_plan(conn, deviation_detected=True, target_step_id=child,
                                 justification="fixed", new_sub_steps=["recovery"])
    db.update_step_status(conn, f"{child}.1", "COMPLETED", set_completed=True)
    return review, child


def test_fl030_reopened_close_refuses_while_registry_is_behind_head(conn, tmp_path):
    repo, head = _git_repo(tmp_path)
    reg = _registry_for(tmp_path, repo, "0000000000000000000000000000000000000000")
    review, child = _recovered(conn, reg)
    out = api.review_and_complete(conn, "P1", registry_path=reg)
    assert out["ready"] is False and out["needs_agent_review"] is True
    assert "registry behind HEAD" in out["reason"]
    plan = db.get_plan(conn, "P1")
    assert plan["status"] == "FAILED"                      # untouched: still the failed close, not COMPLETED
    assert "registry behind HEAD" in (db.get_step(conn, review)["log_context"] or "")
    # the agent refreshes (registry now at HEAD) -> the same call closes
    reg2 = _registry_for(tmp_path, repo, head)
    out = api.review_and_complete(conn, "P1", registry_path=reg2)
    assert out["ready"] is True and out["plan_status"] == "COMPLETED" and out["reopened"] is True
    log = db.get_step(conn, review)["log_context"] or ""
    assert "[REASON SLOTS]" in log and "[REVIEW REOPENED]" in log


def test_fl030_reopened_close_logs_the_reason_slots_when_sha_matches(conn, tmp_path):
    repo, head = _git_repo(tmp_path)
    reg = _registry_for(tmp_path, repo, head)
    review, child = _recovered(conn, reg)
    out = api.review_and_complete(conn, "P1", registry_path=reg)
    assert out["ready"] is True and out["reopened"] is True
    assert "[REASON SLOTS]" in (db.get_step(conn, review)["log_context"] or "")


def test_fl030_no_repo_means_no_sha_check(conn, tmp_path):
    """A registry entry whose repo is not a git checkout cannot be checked — the
    close proceeds as before (the reason is logged, never a silent skip)."""
    reg = _registry(tmp_path)                      # repo=/x
    review, child = _recovered(conn, reg)
    out = api.review_and_complete(conn, "P1", registry_path=reg)
    assert out["ready"] is True and out["plan_status"] == "COMPLETED"
    assert "sha check skipped" in (db.get_step(conn, review)["log_context"] or "")
