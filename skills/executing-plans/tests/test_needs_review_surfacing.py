"""Tests that _apply_op surfaces needs_agent_review onto the op result.

When a plan mentions a registered project and its last regular step completes,
api.review_and_complete returns needs_agent_review=True + project. The
executing-plans complete-step/fail-step path must copy these onto the result so
the script's tail-line JSON shows them to the main agent.

Isolation: PSG_REGISTRY_PATH points at a tmp projects.json — never the real one.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

ORCH_ROOT = Path.home() / "skill-workspace" / "orchestrator"
sys.path.insert(0, str(ORCH_ROOT))

from orchestrator import db as orch_db  # noqa: E402


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
    """Publish a 1-step plan with the given goal via the real publish-plan flow."""
    from conftest import WRITING_PLANS_PUBLISH  # type: ignore
    import subprocess
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


def test_complete_step_surfaces_needs_agent_review(tmp_db, tmp_path, run_script_fn):
    reg = _write_registry(tmp_path, "demo-app")
    plan = _publish(tmp_db, tmp_path, "Refactor the demo-app pipeline")
    plan_id = plan["plan_id"]
    step_id = plan["step_ids"][0]

    env_extra = {"PSG_REGISTRY_PATH": str(reg)}
    # start then complete the only regular step
    run_script_fn("start-step", {"step_id": step_id}, tmp_db, env_extra=env_extra)
    proc = run_script_fn("complete-step", {"step_id": step_id, "summary": "done"},
                         tmp_db, env_extra=env_extra)

    assert proc.returncode == 0, proc.stderr
    # tail-line JSON must carry the agent-review signal
    last = proc.stdout.strip().splitlines()[-1]
    payload = json.loads(last)
    assert payload.get("needs_agent_review") is True, proc.stdout
    assert payload.get("project") == "demo-app", proc.stdout

    # plan must remain IN_PROGRESS, review step NEEDS_REVIEW
    conn = sqlite3.connect(str(tmp_db))
    conn.row_factory = sqlite3.Row
    plan_row = conn.execute("SELECT status FROM Plans WHERE plan_id = ?", (plan_id,)).fetchone()
    review = conn.execute(
        "SELECT status FROM Steps WHERE plan_id = ? AND is_review = 1", (plan_id,)
    ).fetchone()
    conn.close()
    assert plan_row["status"] == "IN_PROGRESS"
    assert review["status"] == "NEEDS_REVIEW"


def test_complete_step_unregistered_closes_as_before(tmp_db, tmp_path, run_script_fn):
    reg = _write_registry(tmp_path, "demo-app")
    plan = _publish(tmp_db, tmp_path, "Build an unrelated standalone widget")
    plan_id = plan["plan_id"]
    step_id = plan["step_ids"][0]

    env_extra = {"PSG_REGISTRY_PATH": str(reg)}
    run_script_fn("start-step", {"step_id": step_id}, tmp_db, env_extra=env_extra)
    proc = run_script_fn("complete-step", {"step_id": step_id, "summary": "done"},
                         tmp_db, env_extra=env_extra)

    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout.strip().splitlines()[-1])
    assert payload.get("needs_agent_review") in (None, False)

    conn = sqlite3.connect(str(tmp_db))
    conn.row_factory = sqlite3.Row
    plan_row = conn.execute("SELECT status FROM Plans WHERE plan_id = ?", (plan_id,)).fetchone()
    conn.close()
    assert plan_row["status"] == "COMPLETED"
