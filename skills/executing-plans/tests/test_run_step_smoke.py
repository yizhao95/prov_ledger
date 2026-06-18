"""End-to-end smoke tests for run-step.sh.

Validates the wrapper that bundles start-step + shell exec + complete/fail-step
with auto-captured log_context. Same fixture pattern as other smoke tests:
ephemeral DB + real publish-plan + subprocess invocations of the script.

Coverage:
  - test_success            success path: log captured, step COMPLETED
  - test_failure            non-zero exit: log captured, step FAILED, fail-step called
  - test_truncation         large output: log truncated to cap + marker present
  - test_auto_review_fires  last step: plan auto-COMPLETED via review-and-complete
  - test_combined_streams   stderr-only command: stderr captured in log_context
  - test_kickoff_banner     start-step's log_context contains "[run-step] kickoff"
"""
from __future__ import annotations

import sqlite3
from pathlib import Path


def _row(db_path: Path, step_id: str) -> dict:
    """Fetch a Step row as a dict — small helper to keep assertions readable."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.execute(
            "SELECT step_id, status, log_context, summary FROM Steps WHERE step_id=?",
            (step_id,),
        )
        r = cur.fetchone()
        return dict(r) if r else {}
    finally:
        conn.close()


def _plan_status(db_path: Path, plan_id: str) -> str:
    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.execute("SELECT status FROM Plans WHERE plan_id=?", (plan_id,))
        return cur.fetchone()[0]
    finally:
        conn.close()


def test_success(run_script_fn, seeded_plan, tmp_db):
    """Success path: exit 0 → step COMPLETED, command stdout in log_context."""
    step_id = seeded_plan["step_ids"][0]
    result = run_script_fn(
        "run-step",
        {"step_id": step_id, "type": "COMMAND", "command": "echo hello-world"},
        tmp_db,
    )
    assert result.returncode == 0, f"run-step should exit 0, got {result.returncode}: {result.stderr}"
    row = _row(tmp_db, step_id)
    assert row["status"] == "COMPLETED", f"expected COMPLETED, got {row['status']}"
    assert "hello-world" in row["log_context"], (
        f"command stdout should be captured in log_context, got: {row['log_context']!r}"
    )
    assert "exit_code=0" in row["log_context"], "exit-code footer must be present"


def test_failure(run_script_fn, seeded_plan, tmp_db):
    """Non-zero exit → step FAILED, exit code in log_context, fail-step path taken."""
    step_id = seeded_plan["step_ids"][0]
    result = run_script_fn(
        "run-step",
        {"step_id": step_id, "type": "COMMAND",
         "command": "echo before-fail; exit 7"},
        tmp_db,
    )
    assert result.returncode == 7, f"run-step must propagate wrapped exit code 7, got {result.returncode}"
    row = _row(tmp_db, step_id)
    assert row["status"] == "FAILED", f"expected FAILED, got {row['status']}"
    assert "before-fail" in row["log_context"], "pre-exit output should be in log_context"
    assert "exit_code=7" in row["log_context"], "exit code 7 should be recorded in log_context"


def test_truncation(run_script_fn, seeded_plan, tmp_db):
    """Output > 16 KiB → log_context truncated with marker, head + tail preserved."""
    step_id = seeded_plan["step_ids"][0]
    # Generate ~32 KiB of distinct head/tail markers we can grep for
    cmd = (
        "python3 -c \""
        "import sys; "
        "sys.stdout.write('AAA' + 'X'*30000 + 'ZZZ')\""
    )
    result = run_script_fn(
        "run-step",
        {"step_id": step_id, "type": "COMMAND", "command": cmd},
        tmp_db,
    )
    assert result.returncode == 0, f"run-step should exit 0: {result.stderr}"
    row = _row(tmp_db, step_id)
    log = row["log_context"]
    assert "TRUNCATED" in log, "truncation marker must appear when over cap"
    assert "AAA" in log, "head 'AAA' marker must survive truncation"
    assert "ZZZ" in log, "tail 'ZZZ' marker must survive truncation"
    # Two layers truncate this payload: (1) _truncate_log.py caps at 16 KiB, (2) api.append_log
    # adds its own [summarized — original was N chars] wrapper. Total stored can be ~25 KiB
    # but the 'X'*30000 middle slab must NOT survive in full.
    assert "X" * 25000 not in log, "middle slab of 25k X's must be cut by truncation"
    assert len(log) < 30000, f"log should be capped well below original 30k+ payload, got {len(log)} bytes"


def test_auto_review_fires(run_script_fn, seeded_plan, tmp_db):
    """Running ALL steps via run-step.sh should auto-COMPLETE the plan (migration 006)."""
    for sid in seeded_plan["step_ids"]:
        result = run_script_fn(
            "run-step",
            {"step_id": sid, "type": "COMMAND", "command": f"echo done-{sid}"},
            tmp_db,
        )
        assert result.returncode == 0, f"step {sid} failed: {result.stderr}"

    status = _plan_status(tmp_db, seeded_plan["plan_id"])
    assert status == "COMPLETED", f"plan should auto-COMPLETE via review-and-complete, got {status}"


def test_combined_streams(run_script_fn, seeded_plan, tmp_db):
    """stderr (not just stdout) must be captured into log_context."""
    step_id = seeded_plan["step_ids"][0]
    result = run_script_fn(
        "run-step",
        {"step_id": step_id, "type": "COMMAND",
         "command": "echo OUT-stream; echo ERR-stream >&2"},
        tmp_db,
    )
    assert result.returncode == 0
    log = _row(tmp_db, step_id)["log_context"]
    assert "OUT-stream" in log, "stdout must be in log_context"
    assert "ERR-stream" in log, "stderr must be in log_context (2>&1 merge)"


def test_kickoff_banner(run_script_fn, seeded_plan, tmp_db):
    """start-step must seed log_context with a [run-step] kickoff banner."""
    step_id = seeded_plan["step_ids"][0]
    result = run_script_fn(
        "run-step",
        {"step_id": step_id, "type": "COMMAND", "command": "true"},
        tmp_db,
    )
    assert result.returncode == 0
    log = _row(tmp_db, step_id)["log_context"]
    assert "[run-step] kickoff" in log, (
        f"kickoff banner missing from log_context: {log!r}"
    )
