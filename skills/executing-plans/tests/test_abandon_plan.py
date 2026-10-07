"""abandon-plan.sh — a plan nobody started can be put down, with a reason (FL-216).

A publish that failed half-way used to leave a plan IN_PROGRESS with no steps,
and nothing could close it: finish-plan would mark it COMPLETED, which is a lie.
abandon-plan turns a plan whose steps never left PENDING into ABANDONED, and,
like raise-budget, records the reason on the plan's own trail (a Deviations row)
without spending the revision budget. A plan with a started step is refused:
abandoning work that ran would hide it.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path


def _plan(db_path, plan_id):
    c = sqlite3.connect(str(db_path)); c.row_factory = sqlite3.Row
    try:
        return dict(c.execute("SELECT * FROM Plans WHERE plan_id = ?", (plan_id,)).fetchone())
    finally:
        c.close()


def _deviations(db_path, plan_id):
    c = sqlite3.connect(str(db_path)); c.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in c.execute("SELECT * FROM Deviations WHERE plan_id = ?", (plan_id,))]
    finally:
        c.close()


def test_a_plan_nobody_started_is_abandoned_with_its_reason(seeded_plan, tmp_db, run_script_fn):
    pid = seeded_plan["plan_id"]
    r = run_script_fn("abandon-plan", {"plan_id": pid, "reason": "published with the wrong goal"}, tmp_db)
    assert r.returncode == 0, r.stderr
    marker = json.loads(r.stdout.splitlines()[-1])
    assert marker["ok"] is True and marker["op"] == "abandon-plan" and marker["plan_id"] == pid
    plan = _plan(tmp_db, pid)
    assert plan["status"] == "ABANDONED" and plan["revision_count"] == 0
    devs = _deviations(tmp_db, pid)
    assert len(devs) == 1 and devs[0]["target_step_id"] is None
    assert devs[0]["justification"].startswith("[ABANDONED]") and "wrong goal" in devs[0]["justification"]


def test_a_plan_left_with_no_steps_by_a_failed_publish_can_be_abandoned(tmp_db, run_script_fn):
    sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "orchestrator-backend"))
    from orchestrator import db
    conn = db.open_db(tmp_db)
    db.insert_plan(conn, "orphan-1", "g")          # what a half-done publish left behind
    conn.close()
    r = run_script_fn("abandon-plan", {"plan_id": "orphan-1", "reason": "publish failed after the plan row"}, tmp_db)
    assert r.returncode == 0, r.stderr
    assert _plan(tmp_db, "orphan-1")["status"] == "ABANDONED"


def test_a_plan_with_a_started_step_is_refused(seeded_plan, tmp_db, run_script_fn):
    pid, first = seeded_plan["plan_id"], seeded_plan["step_ids"][0]
    assert run_script_fn("start-step", {"step_id": first}, tmp_db).returncode == 0
    r = run_script_fn("abandon-plan", {"plan_id": pid, "reason": "changed my mind"}, tmp_db)
    assert r.returncode != 0 and first in r.stderr
    assert _plan(tmp_db, pid)["status"] == "IN_PROGRESS" and _deviations(tmp_db, pid) == []


def test_a_blank_reason_is_refused(seeded_plan, tmp_db, run_script_fn):
    pid = seeded_plan["plan_id"]
    r = run_script_fn("abandon-plan", {"plan_id": pid, "reason": "  "}, tmp_db)
    assert r.returncode != 0 and "reason" in r.stderr
    assert _plan(tmp_db, pid)["status"] == "IN_PROGRESS"
