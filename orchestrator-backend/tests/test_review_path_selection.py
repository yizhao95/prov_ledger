"""Tests for review_and_complete's project-aware path selection.

When all sibling steps are terminal:
  (a) plan mentions a REGISTERED project -> review step -> NEEDS_REVIEW,
      plan stays IN_PROGRESS, result.needs_agent_review = True + project name.
  (b) plan mentions NO registered project -> existing behavior (review COMPLETED,
      plan COMPLETED).
  (c) idempotency: calling again when review already NEEDS_REVIEW does not
      re-mutate and still reports needs_agent_review.
  (d) a FAILED sibling still propagates FAILED regardless of project mention.
"""
from __future__ import annotations

import json
import sqlite3

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
    """steps: list of (status,) ; returns review_step_id."""
    db.insert_plan(conn, plan_id, goal)
    for i, status in enumerate(steps):
        db.insert_step(conn, f"{plan_id}-{chr(65+i)}", plan_id,
                       f"CODE: work on {goal}", i, status=status)
    return db.insert_review_step(conn, plan_id)


def test_registered_project_routes_to_needs_review(conn, tmp_path):
    reg = _registry(tmp_path, "demo-app")
    review_id = _seed(conn, "p-reg", "Refactor the demo-app pipeline",
                      ["COMPLETED", "COMPLETED"])
    result = api.review_and_complete(conn, "p-reg", registry_path=reg)

    assert result["ready"] is False
    assert result["needs_agent_review"] is True
    assert result["project"] == "demo-app"
    assert result["review_step_id"] == review_id
    # review step flipped to NEEDS_REVIEW, plan stays IN_PROGRESS
    assert db.get_step(conn, review_id)["status"] == "NEEDS_REVIEW"
    assert db.get_plan(conn, "p-reg")["status"] == "IN_PROGRESS"


def test_unregistered_plan_closes_as_before(conn, tmp_path):
    reg = _registry(tmp_path, "demo-app")
    review_id = _seed(conn, "p-unreg", "Build a totally unrelated widget",
                      ["COMPLETED", "COMPLETED"])
    result = api.review_and_complete(conn, "p-unreg", registry_path=reg)

    assert result["ready"] is True
    assert result.get("needs_agent_review") in (None, False)
    assert result["plan_status"] == "COMPLETED"
    assert db.get_step(conn, review_id)["status"] == "COMPLETED"
    assert db.get_plan(conn, "p-unreg")["status"] == "COMPLETED"


def test_idempotent_when_already_needs_review(conn, tmp_path):
    reg = _registry(tmp_path, "demo-app")
    review_id = _seed(conn, "p-idem", "Refactor the demo-app pipeline",
                      ["COMPLETED"])
    first = api.review_and_complete(conn, "p-idem", registry_path=reg)
    assert first["needs_agent_review"] is True
    # second call must not error and must keep NEEDS_REVIEW
    second = api.review_and_complete(conn, "p-idem", registry_path=reg)
    assert second["needs_agent_review"] is True
    assert second["ready"] is False
    assert db.get_step(conn, review_id)["status"] == "NEEDS_REVIEW"
    assert db.get_plan(conn, "p-idem")["status"] == "IN_PROGRESS"


def test_failed_sibling_propagates_failed_even_if_registered(conn, tmp_path):
    reg = _registry(tmp_path, "demo-app")
    review_id = _seed(conn, "p-fail", "Refactor the demo-app pipeline",
                      ["COMPLETED", "FAILED"])
    result = api.review_and_complete(conn, "p-fail", registry_path=reg)

    assert result["ready"] is True
    assert result["plan_status"] == "FAILED"
    assert db.get_step(conn, review_id)["status"] == "FAILED"
    assert db.get_plan(conn, "p-fail")["status"] == "FAILED"


def test_pending_sibling_defers_review(conn, tmp_path):
    reg = _registry(tmp_path, "demo-app")
    _seed(conn, "p-pend", "Refactor the demo-app pipeline",
          ["COMPLETED", "PENDING"])
    result = api.review_and_complete(conn, "p-pend", registry_path=reg)
    assert result["ready"] is False
    assert result.get("needs_agent_review") in (None, False)
    assert db.get_plan(conn, "p-pend")["status"] == "IN_PROGRESS"