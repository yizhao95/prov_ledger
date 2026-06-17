"""Tests for api.detect_registered_project — variance-tolerant project mention scan.

Detection decides whether a plan's completion needs an LLM sub-agent review:
it returns the canonical registered project name if the plan goal or any
non-review step description mentions a registered project under ANY spelling
variance, else None.
"""
import json

from orchestrator import api, db


def _registry(tmp_path, *names):
    """Write a projects.json with the given project names; return its path."""
    p = tmp_path / "projects.json"
    p.write_text(json.dumps({
        "projects": [
            {"name": n, "repo": f"/repos/{n}", "db_path": f"/g/{n}.db",
             "commit_sha": "abc", "updated_at": "2026-06-04T00:00:00+00:00"}
            for n in names
        ]
    }))
    return str(p)


def _plan_with_text(conn, plan_id, goal, step_descs):
    db.insert_plan(conn, plan_id, goal)
    for i, d in enumerate(step_descs):
        db.insert_step(conn, f"{plan_id}-{chr(65+i)}", plan_id, d, i)


VARIANCES = [
    "demo-app", "demo app", "demo app", "demoapp",
    "demo_app", "Demo App", "DEMOAPP", "Demo-App",
]


def test_detects_each_variance_in_goal(conn, tmp_path):
    reg = _registry(tmp_path, "demo-app")
    for i, v in enumerate(VARIANCES):
        pid = f"plan-goal-{i}"
        _plan_with_text(conn, pid, f"Refactor the {v} pipeline", ["CODE: do thing"])
        assert api.detect_registered_project(conn, pid, registry_path=reg) == "demo-app", v


def test_detects_variance_in_step_description(conn, tmp_path):
    reg = _registry(tmp_path, "demo-app")
    _plan_with_text(conn, "plan-step", "Generic goal with no project",
                    ["ANALYSIS: read code", "CODE: rename a func in demo_app core"])
    assert api.detect_registered_project(conn, "plan-step", registry_path=reg) == "demo-app"


def test_no_match_returns_none(conn, tmp_path):
    reg = _registry(tmp_path, "demo-app", "billing-svc")
    _plan_with_text(conn, "plan-none", "Build a brand new unrelated thing",
                    ["CODE: implement the unrelated thing"])
    assert api.detect_registered_project(conn, "plan-none", registry_path=reg) is None


def test_empty_registry_returns_none(conn, tmp_path):
    reg = _registry(tmp_path)  # no projects
    _plan_with_text(conn, "plan-empty", "Refactor the demo-app pipeline",
                    ["CODE: do thing"])
    assert api.detect_registered_project(conn, "plan-empty", registry_path=reg) is None


def test_missing_registry_file_returns_none(conn, tmp_path):
    missing = str(tmp_path / "does-not-exist.json")
    _plan_with_text(conn, "plan-missing", "Refactor the demo-app pipeline",
                    ["CODE: do thing"])
    assert api.detect_registered_project(conn, "plan-missing", registry_path=missing) is None


def test_only_non_review_steps_scanned(conn, tmp_path):
    """A registered name appearing ONLY in the review step must not trigger."""
    reg = _registry(tmp_path, "demo-app")
    db.insert_plan(conn, "plan-rev", "Generic goal")
    db.insert_step(conn, "plan-rev-A", "plan-rev", "CODE: unrelated work", 0)
    review_id = db.insert_review_step(conn, "plan-rev")
    # review step description mentions the project but should be ignored
    db.update_step_log  # no-op ref to keep import used
    conn.execute("UPDATE Steps SET description = ? WHERE step_id = ?",
                 ("review the demo-app graph", review_id))
    conn.commit()
    assert api.detect_registered_project(conn, "plan-rev", registry_path=reg) is None


def test_returns_canonical_name_not_variance(conn, tmp_path):
    reg = _registry(tmp_path, "demo-app")
    _plan_with_text(conn, "plan-canon", "Touch DEMOAPP internals", ["CODE: x"])
    result = api.detect_registered_project(conn, "plan-canon", registry_path=reg)
    assert result == "demo-app"  # canonical, not 'DEMOAPP'
