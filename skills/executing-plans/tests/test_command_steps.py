"""A COMMAND step is run through run-step.sh, and never left IN_PROGRESS (FL-214).

complete-step refuses a COMMAND step (its evidence is run-step's exit code), and
run-step refuses to start a step that is already IN_PROGRESS. So a COMMAND step
started by hand had no way out but fail-step and a deviation; and run-step
stopped by a signal — a Bash tool timeout, a Ctrl-C — died with its step
IN_PROGRESS. start-step now refuses a COMMAND step unless run-step is starting
it, and run-step fails its step when it is stopped.
"""
from __future__ import annotations

import json
import os
import signal
import sqlite3
import subprocess
import tempfile
import time


def _step(db_path, step_id):
    c = sqlite3.connect(str(db_path)); c.row_factory = sqlite3.Row
    try:
        return dict(c.execute("SELECT status, failure_reason, step_type FROM Steps WHERE step_id = ?", (step_id,)).fetchone())
    finally:
        c.close()


def test_start_step_refuses_a_command_step_and_names_run_step(seeded_plan, tmp_db, run_script_fn):
    sid = seeded_plan["step_ids"][0]
    r = run_script_fn("start-step", {"step_id": sid, "type": "COMMAND"}, tmp_db)
    assert r.returncode != 0 and "run-step.sh" in r.stderr
    assert _step(tmp_db, sid)["status"] == "PENDING"


def test_a_step_published_as_command_is_refused_too(seeded_plan, tmp_db, run_script_fn):
    sid = seeded_plan["step_ids"][0]
    c = sqlite3.connect(str(tmp_db)); c.execute("UPDATE Steps SET step_type = 'COMMAND' WHERE step_id = ?", (sid,)); c.commit(); c.close()
    r = run_script_fn("start-step", {"step_id": sid}, tmp_db)
    assert r.returncode != 0 and "run-step.sh" in r.stderr


def test_run_step_still_runs_a_command_step(seeded_plan, tmp_db, run_script_fn):
    sid = seeded_plan["step_ids"][0]
    r = run_script_fn("run-step", {"step_id": sid, "type": "COMMAND", "command": "echo ok"}, tmp_db)
    assert r.returncode == 0, r.stderr
    assert _step(tmp_db, sid)["status"] == "COMPLETED"


def test_run_step_stopped_by_a_signal_fails_its_step(seeded_plan, tmp_db, scripts_dir):
    sid = seeded_plan["step_ids"][0]
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump({"step_id": sid, "type": "COMMAND", "command": "sleep 30"}, f)
    env = {**os.environ, "ORCH_DB": str(tmp_db)}
    p = subprocess.Popen(["bash", str(scripts_dir / "run-step.sh"), f.name], env=env, start_new_session=True,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    for _ in range(100):
        if _step(tmp_db, sid)["status"] == "IN_PROGRESS":
            break
        time.sleep(0.1)
    time.sleep(0.3)
    os.killpg(p.pid, signal.SIGTERM)          # what a Bash tool timeout or a Ctrl-C does
    p.wait(timeout=30)
    step = _step(tmp_db, sid)
    assert step["status"] == "FAILED", step
    assert "stopped" in (step["failure_reason"] or "")
