"""Tests for the tracked child SUB_AGENT review step (2026-06-05 follow-up).

When review_and_complete decides a registered-project plan needs an LLM review,
it must:
  1. flip the review step to NEEDS_REVIEW + leave plan IN_PROGRESS,
  2. increment the plan revision,
  3. insert a child SUB_AGENT step id=<review>.1 (PENDING, depth 1),
  4. surface needs_agent_review + review_child_step_id,
  5. be idempotent (no <review>.2, no double revision bump),
and then auto-close the plan from the child's terminal outcome:
  - child COMPLETED -> review COMPLETED + plan COMPLETED
  - child FAILED    -> review FAILED + plan FAILED
Unregistered plans keep the old childless behavior (regression).
"""
from __future__ import annotations

import json

from orchestrator import api, db


def _registry(tmp_path, *names):
    p = tmp_path / "projects.json"
    p.write_text(json.dumps({
        "projects": [
            {"name": n, "repo": f"/repos/{n}", "db_path": f"/g/{n}.db",
             "commit_sha": "abc", "updated_at": "2026-06-04T00:00:00+00:00"}
            for n in names
        ]
    }))
    return str(p)


def _seed(conn, plan_id, goal, steps):
    db.insert_plan(conn, plan_id, goal)
    for i, status in enumerate(steps):
        db.insert_step(conn, f"{plan_id}-{chr(65+i)}", plan_id,
                       f"CODE: work on {goal}", i, status=status)
    return db.insert_review_step(conn, plan_id)


# ── child creation ───────────────────────────────────────────────────────────
def test_registered_plan_creates_child_subagent_step(conn, tmp_path):
    reg = _registry(tmp_path, "demo-app")
    review_id = _seed(conn, "p-child", "Refactor the demo-app pipeline",
                      ["COMPLETED", "COMPLETED"])
    plan_before = db.get_plan(conn, "p-child")["revision_count"]

    result = api.review_and_complete(conn, "p-child", registry_path=reg)

    assert result["ready"] is False
    assert result["needs_agent_review"] is True
    child_id = f"{review_id}.1"
    assert result["review_child_step_id"] == child_id

    child = db.get_step(conn, child_id)
    assert child is not None, "child SUB_AGENT step must be created"
    assert child["status"] == "PENDING"
    assert db.get_step(conn, review_id)["status"] == "NEEDS_REVIEW"
    assert db.get_plan(conn, "p-child")["status"] == "IN_PROGRESS"
    # revision bumped
    assert db.get_plan(conn, "p-child")["revision_count"] == plan_before + 1


def test_child_step_has_correct_attributes(conn, tmp_path):
    reg = _registry(tmp_path, "demo-app")
    review_id = _seed(conn, "p-attr", "Refactor the demo-app pipeline",
                      ["COMPLETED"])
    api.review_and_complete(conn, "p-attr", registry_path=reg)

    child = db.get_step(conn, f"{review_id}.1")
    assert child["parent_step_id"] == review_id
    assert child["depth_level"] == 1
    assert child["step_type"] == "SUB_AGENT"
    assert child["is_review"] == 0


def test_child_creation_is_idempotent(conn, tmp_path):
    reg = _registry(tmp_path, "demo-app")
    review_id = _seed(conn, "p-idem2", "Refactor the demo-app pipeline",
                      ["COMPLETED"])
    api.review_and_complete(conn, "p-idem2", registry_path=reg)
    rev_after_first = db.get_plan(conn, "p-idem2")["revision_count"]

    # second call: no <review>.2, no extra revision bump
    api.review_and_complete(conn, "p-idem2", registry_path=reg)

    assert db.get_step(conn, f"{review_id}.2") is None
    assert len(db.get_children(conn, review_id)) == 1
    assert db.get_plan(conn, "p-idem2")["revision_count"] == rev_after_first


# ── child-outcome auto-close ─────────────────────────────────────────────────
def test_child_completed_closes_plan_completed(conn, tmp_path):
    reg = _registry(tmp_path, "demo-app")
    review_id = _seed(conn, "p-pass", "Refactor the demo-app pipeline",
                      ["COMPLETED"])
    api.review_and_complete(conn, "p-pass", registry_path=reg)
    child_id = f"{review_id}.1"
    # sub-agent does the review and completes its child step
    api.start_step(conn, child_id)
    api.complete_step(conn, child_id)

    result = api.review_and_complete(conn, "p-pass", registry_path=reg)

    assert result["ready"] is True
    assert result["plan_status"] == "COMPLETED"
    assert db.get_step(conn, review_id)["status"] == "COMPLETED"
    assert db.get_plan(conn, "p-pass")["status"] == "COMPLETED"


def test_child_failed_closes_plan_failed(conn, tmp_path):
    reg = _registry(tmp_path, "demo-app")
    review_id = _seed(conn, "p-gap", "Refactor the demo-app pipeline",
                      ["COMPLETED"])
    api.review_and_complete(conn, "p-gap", registry_path=reg)
    child_id = f"{review_id}.1"
    api.start_step(conn, child_id)
    api.fail_step(conn, child_id, reason="stale reference found")

    result = api.review_and_complete(conn, "p-gap", registry_path=reg)

    assert result["ready"] is True
    assert result["plan_status"] == "FAILED"
    assert db.get_step(conn, review_id)["status"] == "FAILED"
    assert db.get_plan(conn, "p-gap")["status"] == "FAILED"


def test_pending_child_keeps_plan_open(conn, tmp_path):
    reg = _registry(tmp_path, "demo-app")
    review_id = _seed(conn, "p-wait", "Refactor the demo-app pipeline",
                      ["COMPLETED"])
    api.review_and_complete(conn, "p-wait", registry_path=reg)
    # child still PENDING -> no-op, still awaiting agent
    result = api.review_and_complete(conn, "p-wait", registry_path=reg)
    assert result["ready"] is False
    assert result["needs_agent_review"] is True
    assert result["review_child_step_id"] == f"{review_id}.1"
    assert db.get_plan(conn, "p-wait")["status"] == "IN_PROGRESS"


# ── regression: unregistered plan keeps childless behavior ───────────────────
def test_unregistered_plan_has_no_child(conn, tmp_path):
    reg = _registry(tmp_path, "demo-app")
    review_id = _seed(conn, "p-none", "Build an unrelated widget",
                      ["COMPLETED", "COMPLETED"])
    result = api.review_and_complete(conn, "p-none", registry_path=reg)

    assert result["ready"] is True
    assert result["plan_status"] == "COMPLETED"
    assert db.get_step(conn, f"{review_id}.1") is None
    assert db.get_step(conn, review_id)["status"] == "COMPLETED"
