"""FL-024: finish-plan's force-finish escape hatch must NOT complete a plan whose
review is awaiting an agent. Once the deterministic procedure has flipped the
review step to NEEDS_REVIEW and created <plan>-REVIEW.1, the only way forward is
driving that child step — finish-plan exits 7 and leaves every row untouched."""
from __future__ import annotations

import sqlite3

from test_review_child_surfacing import _drive_to_needs_review, _plan_status


def _step_status(tmp_db, step_id):
    conn = sqlite3.connect(str(tmp_db))
    row = conn.execute("SELECT status FROM Steps WHERE step_id = ?", (step_id,)).fetchone()
    conn.close()
    return row[0]


def test_finish_plan_cannot_force_past_needs_review(tmp_db, tmp_path, run_script_fn):
    plan_id, payload, env_extra = _drive_to_needs_review(tmp_db, tmp_path, run_script_fn)
    assert payload.get("needs_agent_review") is True, payload
    review_id, child_id = payload["review_step_id"], payload["review_child_step_id"]

    r = run_script_fn("finish-plan", {"plan_id": plan_id}, tmp_db, env_extra=env_extra)

    assert r.returncode == 7, (r.returncode, r.stdout, r.stderr)
    assert "NEEDS_REVIEW" in r.stderr and child_id in r.stderr, r.stderr
    assert _plan_status(tmp_db, plan_id) == "IN_PROGRESS"
    assert _step_status(tmp_db, review_id) == "NEEDS_REVIEW"
    assert _step_status(tmp_db, child_id) == "PENDING"
