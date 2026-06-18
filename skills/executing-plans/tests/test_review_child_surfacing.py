"""Tests the tracked child review step surfacing + auto-close via the scripts.

When a registered-project plan's last regular step completes:
  (7) the complete-step tail line surfaces review_child_step_id alongside
      needs_agent_review + project,
and then driving that child step via the deterministic scripts auto-closes the
plan:
  (8) completing the child step -> plan COMPLETED,
  (9) failing the child step    -> plan FAILED.

Isolation: PSG_REGISTRY_PATH points at a tmp projects.json — never the real one.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

ORCH_ROOT = Path.home() / "skill-workspace" / "orchestrator"
sys.path.insert(0, str(ORCH_ROOT))


def _write_registry(tmp_path: Path, *names: str) -> Path:
    p = tmp_path / "projects.json"
    p.write_text(json.dumps({
        "projects": [
            {"name": n, "repo": f"/repos/{n}", "db_path": f"/g/{n}.db",
             "commit_sha": "abc", "updated_at": "2026-06-04T00:00:00+00:00"}
            for n in names
        ]
    }))
    return p


def _publish(tmp_db: Path, tmp_path: Path, goal: str) -> dict:
    from conftest import WRITING_PLANS_PUBLISH  # type: ignore
    input_path = tmp_path / "seed.json"
    input_path.write_text(json.dumps({
        "goal": goal,
        "prefix": "rev",
        "max_revisions": 5,
        "skills": [{"name": "executing-plans", "source": "iron-law"}],
        "steps": [{"description": "CODE: only step"}],
    }))
    env = os.environ.copy()
    env["ORCH_DB"] = str(tmp_db)
    r = subprocess.run(["bash", str(WRITING_PLANS_PUBLISH), str(input_path)],
                       capture_output=True, text=True, env=env, timeout=15)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def _drive_to_needs_review(tmp_db, tmp_path, run_script_fn, goal="Refactor the demo-app pipeline"):
    """Returns (plan_id, review_child_step_id) with the child step PENDING."""
    reg = _write_registry(tmp_path, "demo-app")
    plan = _publish(tmp_db, tmp_path, goal)
    plan_id = plan["plan_id"]
    step_id = plan["step_ids"][0]
    env_extra = {"PSG_REGISTRY_PATH": str(reg)}
    run_script_fn("start-step", {"step_id": step_id}, tmp_db, env_extra=env_extra)
    proc = run_script_fn("complete-step", {"step_id": step_id, "summary": "done"},
                         tmp_db, env_extra=env_extra)
    payload = json.loads(proc.stdout.strip().splitlines()[-1])
    return plan_id, payload, env_extra


def _plan_status(tmp_db, plan_id):
    conn = sqlite3.connect(str(tmp_db))
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT status FROM Plans WHERE plan_id = ?", (plan_id,)).fetchone()
    conn.close()
    return row["status"]


def test_complete_step_surfaces_review_child_step_id(tmp_db, tmp_path, run_script_fn):
    plan_id, payload, _ = _drive_to_needs_review(tmp_db, tmp_path, run_script_fn)
    assert payload.get("needs_agent_review") is True, payload
    assert payload.get("project") == "demo-app", payload
    child_id = payload.get("review_child_step_id")
    assert child_id, f"tail line must carry review_child_step_id: {payload}"
    assert child_id.endswith("-REVIEW.1")
    # the child step actually exists and is PENDING
    conn = sqlite3.connect(str(tmp_db))
    conn.row_factory = sqlite3.Row
    child = conn.execute("SELECT status, step_type FROM Steps WHERE step_id = ?",
                         (child_id,)).fetchone()
    conn.close()
    assert child is not None
    assert child["status"] == "PENDING"
    assert child["step_type"] == "SUB_AGENT"


def test_completing_child_step_closes_plan_completed(tmp_db, tmp_path, run_script_fn):
    plan_id, payload, env_extra = _drive_to_needs_review(tmp_db, tmp_path, run_script_fn)
    child_id = payload["review_child_step_id"]
    run_script_fn("start-step", {"step_id": child_id}, tmp_db, env_extra=env_extra)
    proc = run_script_fn("complete-step",
                         {"step_id": child_id, "summary": "review clean"},
                         tmp_db, env_extra=env_extra)
    assert proc.returncode == 0, proc.stderr
    assert _plan_status(tmp_db, plan_id) == "COMPLETED"


def test_failing_child_step_closes_plan_failed(tmp_db, tmp_path, run_script_fn):
    plan_id, payload, env_extra = _drive_to_needs_review(tmp_db, tmp_path, run_script_fn)
    child_id = payload["review_child_step_id"]
    run_script_fn("start-step", {"step_id": child_id}, tmp_db, env_extra=env_extra)
    proc = run_script_fn("fail-step",
                         {"step_id": child_id, "reason": "stale reference found"},
                         tmp_db, env_extra=env_extra)
    assert proc.returncode == 0, proc.stderr
    assert _plan_status(tmp_db, plan_id) == "FAILED"
