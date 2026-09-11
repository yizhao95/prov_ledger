"""Tests for agent-review-close.sh — the sub-agent's finalize entry point.

The update-project-state-graph review sub-agent uses this deterministic flow to
close a NEEDS_REVIEW plan:
  - outcome 'pass' -> review step NEEDS_REVIEW -> COMPLETED, plan COMPLETED
  - outcome 'fail' -> review step NEEDS_REVIEW -> FAILED, plan FAILED (details logged)
  - missing plan_id / outcome, or invalid outcome -> non-zero exit, no mutation
  - emits the single-line OK marker as the final stdout line on success

Helpers drive a plan to NEEDS_REVIEW by completing its only regular step with a
registered project mentioned (PSG_REGISTRY_PATH isolation).
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

ORCH_ROOT = Path.home() / "skill-workspace" / "orchestrator"
sys.path.insert(0, str(ORCH_ROOT))

from orchestrator import db as orch_db  # noqa: E402


def _registry(tmp_path: Path, *names: str) -> Path:
    p = tmp_path / "projects.json"
    p.write_text(json.dumps({
        "projects": [{"name": n, "repo": f"/repos/{n}", "db_path": f"/g/{n}.db",
                      "commit_sha": "abc", "updated_at": "2026-06-04T00:00:00+00:00"}
                     for n in names]
    }))
    return p


def _publish(tmp_db: Path, tmp_path: Path, goal: str) -> dict:
    from conftest import WRITING_PLANS_PUBLISH  # type: ignore
    input_path = tmp_path / "seed.json"
    input_path.write_text(json.dumps({
        "goal": goal, "prefix": "rev", "max_revisions": 5,
        "skills": [{"name": "executing-plans", "source": "iron-law"}],
        "steps": [{"description": "CODE: only step"}],
    }))
    env = os.environ.copy()
    env["ORCH_DB"] = str(tmp_db)
    r = subprocess.run(["bash", str(WRITING_PLANS_PUBLISH), str(input_path)],
                       capture_output=True, text=True, env=env, timeout=15)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def _drive_to_needs_review(tmp_db, tmp_path, run_script_fn) -> tuple[str, str]:
    """Returns (plan_id, review_step_id) with the review step in NEEDS_REVIEW."""
    reg = _registry(tmp_path, "demo-app")
    plan = _publish(tmp_db, tmp_path, "Refactor the demo-app pipeline")
    plan_id = plan["plan_id"]
    step_id = plan["step_ids"][0]
    env_extra = {"PSG_REGISTRY_PATH": str(reg)}
    run_script_fn("start-step", {"step_id": step_id}, tmp_db, env_extra=env_extra)
    run_script_fn("complete-step", {"step_id": step_id, "summary": "x"}, tmp_db,
                  env_extra=env_extra)
    conn = sqlite3.connect(str(tmp_db))
    conn.row_factory = sqlite3.Row
    rid = conn.execute("SELECT step_id FROM Steps WHERE plan_id=? AND is_review=1",
                       (plan_id,)).fetchone()["step_id"]
    status = conn.execute("SELECT status FROM Steps WHERE step_id=?", (rid,)).fetchone()["status"]
    conn.close()
    assert status == "NEEDS_REVIEW", f"setup failed; review status={status}"
    return plan_id, rid


def _status(tmp_db, plan_id, review_id):
    conn = sqlite3.connect(str(tmp_db))
    conn.row_factory = sqlite3.Row
    p = conn.execute("SELECT status FROM Plans WHERE plan_id=?", (plan_id,)).fetchone()["status"]
    r = conn.execute("SELECT status FROM Steps WHERE step_id=?", (review_id,)).fetchone()["status"]
    conn.close()
    return p, r


def test_pass_closes_completed(tmp_db, tmp_path, run_script_fn):
    plan_id, rid = _drive_to_needs_review(tmp_db, tmp_path, run_script_fn)
    proc = run_script_fn("agent-review-close",
                         {"plan_id": plan_id, "outcome": "pass",
                          "summary": "clean: graph refreshed, tests green",
                          "log_context": "review_diff: ok=True"},
                         tmp_db)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout.strip().splitlines()[-1])["ok"] is True
    assert _status(tmp_db, plan_id, rid) == ("COMPLETED", "COMPLETED")


def test_fail_closes_failed_with_details(tmp_db, tmp_path, run_script_fn):
    plan_id, rid = _drive_to_needs_review(tmp_db, tmp_path, run_script_fn)
    proc = run_script_fn("agent-review-close",
                         {"plan_id": plan_id, "outcome": "fail",
                          "summary": "stale reference: pipeline calls renamed fn",
                          "log_context": "review_diff: ok=False gaps=[old_fn]"},
                         tmp_db)
    assert proc.returncode == 0, proc.stderr
    assert _status(tmp_db, plan_id, rid) == ("FAILED", "FAILED")
    # details must be persisted on the review step log
    conn = sqlite3.connect(str(tmp_db))
    conn.row_factory = sqlite3.Row
    log = conn.execute("SELECT log_context FROM Steps WHERE step_id=?", (rid,)).fetchone()["log_context"]
    conn.close()
    assert "stale reference" in log or "old_fn" in log


def test_missing_plan_id_rejected(tmp_db, tmp_path, run_script_fn):
    proc = run_script_fn("agent-review-close", {"outcome": "pass"}, tmp_db)
    assert proc.returncode != 0


def test_missing_outcome_rejected(tmp_db, tmp_path, run_script_fn):
    plan_id, _ = _drive_to_needs_review(tmp_db, tmp_path, run_script_fn)
    proc = run_script_fn("agent-review-close", {"plan_id": plan_id}, tmp_db)
    assert proc.returncode != 0


def test_invalid_outcome_rejected(tmp_db, tmp_path, run_script_fn):
    plan_id, rid = _drive_to_needs_review(tmp_db, tmp_path, run_script_fn)
    proc = run_script_fn("agent-review-close",
                         {"plan_id": plan_id, "outcome": "maybe"}, tmp_db)
    assert proc.returncode != 0
    # no mutation: review stays NEEDS_REVIEW
    assert _status(tmp_db, plan_id, rid) == ("IN_PROGRESS", "NEEDS_REVIEW")


# ── FL-019: agent-review-close accepts a FAILED review whose child recovered ──

def _park_and_fail(seeded_plan, tmp_db, run_script_fn, tmp_path):
    """Registered-project plan parked in NEEDS_REVIEW, child REVIEW.1 failed → plan FAILED."""
    reg = tmp_path / "projects.json"
    reg.write_text(json.dumps({"projects": [{"name": "seed", "repo": "/x", "db_path": str(tmp_path / "absent.db"), "commit_sha": "c"}]}))
    env = {"PSG_REGISTRY_PATH": str(reg)}
    pid = seeded_plan["plan_id"]
    for sid in seeded_plan["step_ids"]:
        run_script_fn("start-step", {"step_id": sid, "type": "CODE"}, tmp_db, env_extra=env)
        run_script_fn("complete-step", {"step_id": sid}, tmp_db, env_extra=env)
    child = f"{pid}-REVIEW.1"
    run_script_fn("start-step", {"step_id": child, "type": "SUB_AGENT"}, tmp_db, env_extra=env)
    r = run_script_fn("fail-step", {"step_id": child, "reason": "gate false positive"}, tmp_db, env_extra=env)
    assert r.returncode == 0, r.stderr
    c = sqlite3.connect(str(tmp_db))
    assert c.execute("SELECT status FROM Plans WHERE plan_id=?", (pid,)).fetchone()[0] == "FAILED"
    return pid, child, env


def test_fl019_auto_reopen_when_substep_completes_via_scripts(seeded_plan, tmp_db, run_script_fn, tmp_path):
    """complete-step on the recovery sub-step fires review_and_complete, which
    re-opens the FAILED review and closes the plan COMPLETED by itself."""
    pid, child, env = _park_and_fail(seeded_plan, tmp_db, run_script_fn, tmp_path)
    r = run_script_fn("deviate", {"parent_step_id": child, "justification": "additive kwargs; suite green",
                                  "sub_steps": ["manual verdict + refresh"]}, tmp_db, env_extra=env)
    assert r.returncode == 0, r.stderr
    run_script_fn("start-step", {"step_id": f"{child}.1", "type": "COMMAND"}, tmp_db, env_extra=env)
    r = run_script_fn("complete-step", {"step_id": f"{child}.1", "log_context": "refresh ok\n--- exit_code=0, runtime=1s ---"}, tmp_db, env_extra=env)
    assert r.returncode == 0, r.stderr
    c = sqlite3.connect(str(tmp_db))
    assert c.execute("SELECT status, review_state FROM Plans WHERE plan_id=?", (pid,)).fetchone() == ("COMPLETED", "reviewed")
    assert c.execute("SELECT status FROM Steps WHERE step_id=?", (f"{pid}-REVIEW",)).fetchone()[0] == "COMPLETED"
    assert "[REVIEW REOPENED]" in c.execute("SELECT log_context FROM Steps WHERE step_id=?", (f"{pid}-REVIEW",)).fetchone()[0]


def test_fl019_close_pass_after_recovery(seeded_plan, tmp_db, run_script_fn, tmp_path):
    """The explicit close path: the sub-step was recovered outside the scripts
    (no auto-review fired), then agent-review-close pass finalizes it."""
    pid, child, env = _park_and_fail(seeded_plan, tmp_db, run_script_fn, tmp_path)
    r = run_script_fn("deviate", {"parent_step_id": child, "justification": "additive kwargs; suite green",
                                  "sub_steps": ["manual verdict + refresh"]}, tmp_db, env_extra=env)
    assert r.returncode == 0, r.stderr
    c = sqlite3.connect(str(tmp_db))
    c.execute("UPDATE Steps SET status='COMPLETED', completed_at='2026-09-11 00:00:00' WHERE step_id=?", (f"{child}.1",))
    c.commit(); c.close()
    r = run_script_fn("agent-review-close", {"plan_id": pid, "outcome": "pass", "summary": "recovered"}, tmp_db, env_extra=env)
    assert r.returncode == 0, r.stderr
    c = sqlite3.connect(str(tmp_db))
    assert c.execute("SELECT status, review_state FROM Plans WHERE plan_id=?", (pid,)).fetchone() == ("COMPLETED", "reviewed")
    assert c.execute("SELECT status FROM Steps WHERE step_id=?", (f"{pid}-REVIEW",)).fetchone()[0] == "COMPLETED"
    assert "[AGENT-REVIEW PASS after recovery]" in c.execute("SELECT log_context FROM Steps WHERE step_id=?", (f"{pid}-REVIEW",)).fetchone()[0]


def test_fl019_close_pass_refused_without_recovery(seeded_plan, tmp_db, run_script_fn, tmp_path):
    pid, child, env = _park_and_fail(seeded_plan, tmp_db, run_script_fn, tmp_path)
    r = run_script_fn("agent-review-close", {"plan_id": pid, "outcome": "pass"}, tmp_db, env_extra=env)
    assert r.returncode != 0 and "recover" in (r.stderr + r.stdout).lower()
    assert sqlite3.connect(str(tmp_db)).execute("SELECT status FROM Plans WHERE plan_id=?", (pid,)).fetchone()[0] == "FAILED"
