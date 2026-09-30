"""raise-budget.sh — the revision budget is raised through a flow, with a reason,
and the raise itself becomes a record.

FL-137: the loop breaker used to say "Options: raise max_revisions, restructure
plan, or abandon" while no script could raise it. The only remaining route was a
hand-written UPDATE, which this project forbids. These tests pin the flow that
makes the breaker's advice true.
"""
from __future__ import annotations

import json
import sqlite3


def _plan(db_path, plan_id):
    conn = sqlite3.connect(str(db_path)); conn.row_factory = sqlite3.Row
    try:
        return dict(conn.execute("SELECT * FROM Plans WHERE plan_id = ?", (plan_id,)).fetchone())
    finally:
        conn.close()


def _deviations(db_path, plan_id):
    conn = sqlite3.connect(str(db_path)); conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM Deviations WHERE plan_id = ? ORDER BY deviation_id", (plan_id,))]
    finally:
        conn.close()


def test_raise_budget_lifts_the_ceiling_and_records_why(seeded_plan, tmp_db, run_script_fn):
    pid = seeded_plan["plan_id"]
    assert _plan(tmp_db, pid)["max_revisions"] == 5
    r = run_script_fn("raise-budget", {"plan_id": pid, "new_max": 8,
                                       "reason": "two of the five revisions were coordinator corrections"}, tmp_db)
    assert r.returncode == 0, r.stderr
    marker = json.loads(r.stdout.splitlines()[-1])
    assert marker["ok"] is True and marker["op"] == "raise-budget" and marker["plan_id"] == pid
    plan = _plan(tmp_db, pid)
    assert plan["max_revisions"] == 8
    # the raise is a decision about the plan: it lands in the plan's own record
    devs = _deviations(tmp_db, pid)
    assert len(devs) == 1
    assert devs[0]["target_step_id"] is None
    assert "5" in devs[0]["justification"] and "8" in devs[0]["justification"]
    assert "coordinator corrections" in devs[0]["justification"]


def test_raise_budget_does_not_spend_the_budget_it_raises(seeded_plan, tmp_db, run_script_fn):
    """Raising the ceiling is not a plan revision — revision_count must not move,
    or the raise would consume part of what it just granted."""
    pid = seeded_plan["plan_id"]
    before = _plan(tmp_db, pid)["revision_count"]
    r = run_script_fn("raise-budget", {"plan_id": pid, "new_max": 6, "reason": "one more recovery step"}, tmp_db)
    assert r.returncode == 0, r.stderr
    assert _plan(tmp_db, pid)["revision_count"] == before


def test_raise_budget_requires_a_reason(seeded_plan, tmp_db, run_script_fn):
    pid = seeded_plan["plan_id"]
    for payload in ({"plan_id": pid, "new_max": 8},
                    {"plan_id": pid, "new_max": 8, "reason": ""},
                    {"plan_id": pid, "new_max": 8, "reason": "   "}):
        r = run_script_fn("raise-budget", payload, tmp_db)
        assert r.returncode != 0, payload
        assert "reason" in r.stderr.lower(), r.stderr
        assert _plan(tmp_db, pid)["max_revisions"] == 5          # nothing written
        assert _deviations(tmp_db, pid) == []


def test_raise_budget_refuses_anything_that_is_not_a_raise(seeded_plan, tmp_db, run_script_fn):
    pid = seeded_plan["plan_id"]
    for bad in (5, 4, 0, -1, "eight", 5.5, None):
        r = run_script_fn("raise-budget", {"plan_id": pid, "new_max": bad, "reason": "why"}, tmp_db)
        assert r.returncode != 0, bad
        assert "new_max" in r.stderr, (bad, r.stderr)
    assert _plan(tmp_db, pid)["max_revisions"] == 5
    assert _deviations(tmp_db, pid) == []


def test_raise_budget_refuses_an_unknown_plan(tmp_db, run_script_fn):
    r = run_script_fn("raise-budget", {"plan_id": "no-such-plan", "new_max": 9, "reason": "why"}, tmp_db)
    assert r.returncode != 0 and "no-such-plan" in r.stderr


def test_raise_budget_unblocks_the_deviate_the_breaker_refused(seeded_plan, tmp_db, run_script_fn):
    """The end-to-end point: a plan at 5/5 cannot deviate, the breaker names this
    script, the raise goes through with a reason, and the deviation then lands."""
    pid, steps = seeded_plan["plan_id"], seeded_plan["step_ids"]
    conn = sqlite3.connect(str(tmp_db))
    conn.execute("UPDATE Plans SET revision_count = 5 WHERE plan_id = ?", (pid,))
    conn.commit(); conn.close()
    dev = {"parent_step_id": steps[0], "justification": "one more recovery step",
           "sub_steps": [{"description": "ANALYSIS: retry", "type": "ANALYSIS"}]}
    r = run_script_fn("deviate", dev, tmp_db)
    assert r.returncode != 0
    assert "raise-budget.sh" in r.stderr, f"the breaker must name the script that exists: {r.stderr}"
    r = run_script_fn("raise-budget", {"plan_id": pid, "new_max": 6,
                                       "reason": "the fifth revision was my own mid-flight correction"}, tmp_db)
    assert r.returncode == 0, r.stderr
    r = run_script_fn("deviate", dev, tmp_db)
    assert r.returncode == 0, r.stderr
    assert _plan(tmp_db, pid)["revision_count"] == 6
    assert [d["target_step_id"] for d in _deviations(tmp_db, pid)] == [None, steps[0]]
