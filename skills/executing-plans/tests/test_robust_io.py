"""TDD tests for the I/O hardening fixes (Bug 1 + Bug 2 RCA).

Bug 1: agents using `bash script.sh | tail -1` saw no failure signal because
       _apply_op.py printed multi-line pretty JSON (so tail -1 caught the `}`
       brace, not a parseable success/failure marker).

Bug 2: complete-step / start-step / fail-step had no way to capture raw shell
       output inline — agents had to make a separate append-log.sh call, and
       in practice routinely forgot to.

These tests pin down the new contract:
  - Last non-empty stdout line MUST be a single compact JSON line
    {"ok": true, "op": "<op-name>", "step_id": "<id>", ...}
  - Failure: returncode != 0, stderr has friendly ❌ message (existing behavior)
  - complete-step / start-step / fail-step accept optional `log_context` field
  - Inline log_context is appended to Steps.log_context column with the same
    `---\\n<ts>\\n---\\n<content>\\n` delimiter pattern that append-log.sh uses,
    so count_log_entries() continues to count entries correctly.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path


def _last_json_line(stdout: str) -> dict:
    """Parse the LAST non-empty line of stdout as JSON. Mirrors agent invocation
    pattern: `OUT=$(bash script.sh in.json); echo $OUT | tail -1 | jq .`
    """
    lines = [ln for ln in stdout.splitlines() if ln.strip()]
    assert lines, f"no non-empty stdout lines (full stdout: {stdout!r})"
    last = lines[-1]
    try:
        return json.loads(last)
    except json.JSONDecodeError as e:
        raise AssertionError(
            f"last stdout line is not valid JSON: {last!r}\n"
            f"full stdout: {stdout!r}\n"
            f"json error: {e}"
        )


# ── Bug 1 fixes: single-line OK marker ─────────────────────────────────────

class TestSingleLineOkMarker:
    def test_complete_step_emits_single_line_ok_marker(self, run_script_fn, seeded_plan, tmp_db):
        sid = seeded_plan["step_ids"][0]
        # Need to start it first (state machine)
        r1 = run_script_fn("start-step", {"step_id": sid, "type": "CODE"}, tmp_db)
        assert r1.returncode == 0, f"start-step failed: {r1.stderr}"
        marker = _last_json_line(r1.stdout)
        assert marker.get("ok") is True
        assert marker.get("op") == "start-step"
        assert marker.get("step_id") == sid

        r2 = run_script_fn("complete-step", {"step_id": sid, "summary": "done"}, tmp_db)
        assert r2.returncode == 0, f"complete-step failed: {r2.stderr}"
        marker = _last_json_line(r2.stdout)
        assert marker.get("ok") is True
        assert marker.get("op") == "complete-step"
        assert marker.get("step_id") == sid

    def test_failure_returns_nonzero_and_friendly_stderr(self, run_script_fn, seeded_plan, tmp_db):
        # Try to complete a PENDING step (skipping start) — should fail same way as Bug 1
        sid = seeded_plan["step_ids"][1]
        r = run_script_fn("complete-step", {"step_id": sid, "summary": "x"}, tmp_db)
        assert r.returncode != 0, "should fail when completing a PENDING step"
        assert "❌" in r.stderr or "Forbidden state transition" in r.stderr

    def test_record_skill_emits_marker(self, run_script_fn, seeded_plan, tmp_db):
        r = run_script_fn("record-skill", {
            "plan_id": seeded_plan["plan_id"],
            "name": "test-driven-development",
            "source": "iron-law",
            "reason": "smoke test",
        }, tmp_db)
        assert r.returncode == 0, f"record-skill failed: {r.stderr}"
        marker = _last_json_line(r.stdout)
        assert marker.get("ok") is True
        assert marker.get("op") == "record-skill"


# ── Bug 2 fixes: inline log_context ─────────────────────────────────────────

class TestInlineLogContext:
    def test_complete_step_with_log_context_persists_log(self, run_script_fn, seeded_plan, tmp_db):
        sid = seeded_plan["step_ids"][0]
        run_script_fn("start-step", {"step_id": sid, "type": "CODE"}, tmp_db)

        log_text = "pytest output: ........... 11 passed in 0.04s"
        r = run_script_fn("complete-step", {
            "step_id": sid,
            "summary": "tests pass",
            "log_context": log_text,
        }, tmp_db)
        assert r.returncode == 0, f"complete-step with log_context failed: {r.stderr}"

        # Verify log_context column is populated
        conn = sqlite3.connect(str(tmp_db))
        row = conn.execute("SELECT log_context FROM Steps WHERE step_id=?", (sid,)).fetchone()
        conn.close()
        assert row is not None
        assert row[0], f"log_context should be populated, got: {row[0]!r}"
        assert log_text in row[0], f"log_text not found in stored log: {row[0]!r}"

    def test_start_step_with_log_context_persists_log(self, run_script_fn, seeded_plan, tmp_db):
        sid = seeded_plan["step_ids"][1]
        log_text = "kicking off step B"
        r = run_script_fn("start-step", {
            "step_id": sid,
            "type": "CODE",
            "log_context": log_text,
        }, tmp_db)
        assert r.returncode == 0, f"start-step with log_context failed: {r.stderr}"

        conn = sqlite3.connect(str(tmp_db))
        row = conn.execute("SELECT log_context FROM Steps WHERE step_id=?", (sid,)).fetchone()
        conn.close()
        assert row[0] and log_text in row[0]

    def test_fail_step_with_log_context_persists_log(self, run_script_fn, seeded_plan, tmp_db):
        sid = seeded_plan["step_ids"][2]
        run_script_fn("start-step", {"step_id": sid, "type": "CODE"}, tmp_db)
        log_text = "build broke: ImportError on numpy"
        r = run_script_fn("fail-step", {
            "step_id": sid,
            "reason": "import error",
            "log_context": log_text,
        }, tmp_db)
        assert r.returncode == 0, f"fail-step with log_context failed: {r.stderr}"

        conn = sqlite3.connect(str(tmp_db))
        row = conn.execute("SELECT log_context FROM Steps WHERE step_id=?", (sid,)).fetchone()
        conn.close()
        assert row[0] and log_text in row[0]

    def test_complete_step_without_log_context_unchanged(self, run_script_fn, seeded_plan, tmp_db):
        # Backward compat: no log_context → log_context stays empty (existing behavior)
        sid = seeded_plan["step_ids"][0]
        run_script_fn("start-step", {"step_id": sid, "type": "CODE"}, tmp_db)
        r = run_script_fn("complete-step", {"step_id": sid, "summary": "no log"}, tmp_db)
        assert r.returncode == 0

        conn = sqlite3.connect(str(tmp_db))
        row = conn.execute("SELECT log_context FROM Steps WHERE step_id=?", (sid,)).fetchone()
        conn.close()
        # Should be NULL or empty — NOT have any spurious content
        assert not row[0], f"log_context should be empty, got: {row[0]!r}"

    def test_inline_log_uses_append_log_delimiter_format(self, run_script_fn, seeded_plan, tmp_db):
        """Inline log must use the same `---\\n<ts>\\n---\\n<content>\\n` format as
        append-log.sh, so the dashboard's count_log_entries() helper continues
        to work correctly when entries come from BOTH inline + standalone calls."""
        sid = seeded_plan["step_ids"][0]
        run_script_fn("start-step", {"step_id": sid, "type": "CODE",
                                     "log_context": "first entry from start"}, tmp_db)
        run_script_fn("append-log", {"step_id": sid, "text": "middle entry from append-log"}, tmp_db)
        run_script_fn("complete-step", {"step_id": sid, "summary": "done",
                                        "log_context": "last entry from complete"}, tmp_db)

        conn = sqlite3.connect(str(tmp_db))
        row = conn.execute("SELECT log_context FROM Steps WHERE step_id=?", (sid,)).fetchone()
        conn.close()
        log = row[0]
        # All three entries must be present
        assert "first entry from start" in log
        assert "middle entry from append-log" in log
        assert "last entry from complete" in log
        # Format check: must contain the `---` delimiter (same as append-log)
        assert "---" in log, f"missing --- delimiter, got: {log!r}"
