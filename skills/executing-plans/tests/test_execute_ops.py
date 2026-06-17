"""Per-op pytest cases for the 7 deterministic-flow scripts in executing-plans/scripts/.

Each op class covers:
  - happy path (verifies DB state after)
  - validation failures (missing required field, bogus enum, invalid JSON)
  - state-machine guards (forbidden transitions, circuit breakers)

All tests use ephemeral DB via ORCH_DB env var (see conftest.tmp_db).
NEVER touches ~/skill-workspace/orchestrator.db.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path



# ── 1. start-step ──────────────────────────────────────────────────────────

class TestStartStep:
    def test_happy_path(self, seeded_plan, tmp_db, scripts_dir, run_script_fn):
        step_id = seeded_plan["step_ids"][0]
        r = run_script_fn("start-step", {"step_id": step_id, "type": "ANALYSIS"}, tmp_db)
        assert r.returncode == 0, r.stderr
        # stdout is now: pretty-printed JSON result + final compact OK marker line.
        # Strip the marker line to get back to the pretty result for assertions.
        payload = json.loads("\n".join(r.stdout.splitlines()[:-1]))
        assert payload["status"] == "IN_PROGRESS"
        # Verify in DB
        conn = sqlite3.connect(str(tmp_db))
        row = conn.execute("SELECT status, step_type FROM Steps WHERE step_id=?", (step_id,)).fetchone()
        assert row[0] == "IN_PROGRESS"
        assert row[1] == "ANALYSIS"
        conn.close()

    def test_missing_step_id(self, tmp_db, scripts_dir, run_script_fn):
        r = run_script_fn("start-step", {"type": "ANALYSIS"}, tmp_db)
        assert r.returncode != 0
        assert "step_id" in (r.stderr + r.stdout).lower()

    def test_forbidden_transition(self, seeded_plan, tmp_db, scripts_dir, run_script_fn):
        """COMPLETED step cannot be started again — state machine rejects it."""
        step_id = seeded_plan["step_ids"][0]
        run_script_fn("start-step", {"step_id": step_id}, tmp_db)
        run_script_fn("complete-step", {"step_id": step_id}, tmp_db)
        # now try to start again
        r = run_script_fn("start-step", {"step_id": step_id}, tmp_db)
        assert r.returncode != 0
        assert "transition" in (r.stderr + r.stdout).lower() or "forbidden" in (r.stderr + r.stdout).lower()


# ── 2. complete-step ───────────────────────────────────────────────────────

class TestCompleteStep:
    def test_happy_path_with_summary(self, seeded_plan, tmp_db, scripts_dir, run_script_fn):
        step_id = seeded_plan["step_ids"][0]
        run_script_fn("start-step", {"step_id": step_id}, tmp_db)
        r = run_script_fn("complete-step",
                       {"step_id": step_id, "summary": "did the thing"}, tmp_db)
        assert r.returncode == 0, r.stderr
        # Verify summary persisted
        conn = sqlite3.connect(str(tmp_db))
        row = conn.execute("SELECT status, summary FROM Steps WHERE step_id=?", (step_id,)).fetchone()
        assert row[0] == "COMPLETED"
        assert row[1] == "did the thing"
        conn.close()

    def test_complete_without_starting_rejected(self, seeded_plan, tmp_db, scripts_dir, run_script_fn):
        """Cannot go PENDING → COMPLETED directly."""
        step_id = seeded_plan["step_ids"][0]
        r = run_script_fn("complete-step", {"step_id": step_id}, tmp_db)
        assert r.returncode != 0


# ── 3. fail-step ───────────────────────────────────────────────────────────

class TestFailStep:
    def test_happy_path(self, seeded_plan, tmp_db, scripts_dir, run_script_fn):
        step_id = seeded_plan["step_ids"][0]
        run_script_fn("start-step", {"step_id": step_id}, tmp_db)
        r = run_script_fn("fail-step",
                       {"step_id": step_id, "reason": "test went boom"}, tmp_db)
        assert r.returncode == 0, r.stderr
        conn = sqlite3.connect(str(tmp_db))
        row = conn.execute("SELECT status, log_context FROM Steps WHERE step_id=?", (step_id,)).fetchone()
        assert row[0] == "FAILED"
        assert "test went boom" in (row[1] or "")
        conn.close()


# ── 4. append-log ──────────────────────────────────────────────────────────

class TestAppendLog:
    def test_happy_path(self, seeded_plan, tmp_db, scripts_dir, run_script_fn):
        step_id = seeded_plan["step_ids"][0]
        run_script_fn("start-step", {"step_id": step_id}, tmp_db)
        r = run_script_fn("append-log",
                       {"step_id": step_id, "text": "ran command X, got result Y"}, tmp_db)
        assert r.returncode == 0, r.stderr
        conn = sqlite3.connect(str(tmp_db))
        log = conn.execute("SELECT log_context FROM Steps WHERE step_id=?", (step_id,)).fetchone()[0]
        assert "ran command X" in log
        conn.close()

    def test_appends_not_overwrites(self, seeded_plan, tmp_db, scripts_dir, run_script_fn):
        step_id = seeded_plan["step_ids"][0]
        run_script_fn("start-step", {"step_id": step_id}, tmp_db)
        run_script_fn("append-log", {"step_id": step_id, "text": "first"}, tmp_db)
        run_script_fn("append-log", {"step_id": step_id, "text": "second"}, tmp_db)
        conn = sqlite3.connect(str(tmp_db))
        log = conn.execute("SELECT log_context FROM Steps WHERE step_id=?", (step_id,)).fetchone()[0]
        assert "first" in log and "second" in log
        conn.close()

    def test_missing_text(self, seeded_plan, tmp_db, scripts_dir, run_script_fn):
        step_id = seeded_plan["step_ids"][0]
        r = run_script_fn("append-log", {"step_id": step_id}, tmp_db)
        assert r.returncode != 0
        assert "text" in (r.stderr + r.stdout).lower()


# ── 5. deviate ─────────────────────────────────────────────────────────────

class TestDeviate:
    def test_happy_path_inserts_subs(self, seeded_plan, tmp_db, scripts_dir, run_script_fn):
        parent = seeded_plan["step_ids"][1]
        run_script_fn("start-step", {"step_id": parent}, tmp_db)
        r = run_script_fn("deviate", {
            "parent_step_id": parent,
            "justification": "discovered the parent needs decomposition",
            "sub_steps": ["TEST: write failing test for X", "CODE: implement X"],
        }, tmp_db)
        assert r.returncode == 0, r.stderr
        payload = json.loads("\n".join(r.stdout.splitlines()[:-1]))
        assert payload["accepted"] is True
        assert len(payload["new_step_ids"]) == 2
        # revision_count incremented
        conn = sqlite3.connect(str(tmp_db))
        rc = conn.execute("SELECT revision_count FROM Plans WHERE plan_id=?",
                          (seeded_plan["plan_id"],)).fetchone()[0]
        assert rc == 1
        # sub-steps have parent set
        sub_rows = conn.execute("SELECT step_id, parent_step_id, depth_level FROM Steps WHERE parent_step_id=?",
                                (parent,)).fetchall()
        assert len(sub_rows) == 2
        for r_ in sub_rows:
            assert r_[1] == parent
            assert r_[2] == 1   # depth = parent depth (0) + 1
        conn.close()

    def test_max_revisions_circuit_breaker(self, seeded_plan, tmp_db, scripts_dir, run_script_fn):
        """Hitting max_revisions triggers the breaker — script exits non-zero."""
        plan_id = seeded_plan["plan_id"]
        parent = seeded_plan["step_ids"][1]
        run_script_fn("start-step", {"step_id": parent}, tmp_db)
        # Manually crank revision_count to max - 1 so next deviate trips it
        conn = sqlite3.connect(str(tmp_db))
        conn.execute("UPDATE Plans SET revision_count=5 WHERE plan_id=?", (plan_id,))
        conn.commit()
        conn.close()
        r = run_script_fn("deviate", {
            "parent_step_id": parent,
            "justification": "test breaker",
            "sub_steps": ["x"],
        }, tmp_db)
        assert r.returncode != 0
        assert "revision" in (r.stderr + r.stdout).lower() or "max" in (r.stderr + r.stdout).lower()


# ── 6. record-skill ────────────────────────────────────────────────────────

class TestRecordSkill:
    def test_happy_path(self, seeded_plan, tmp_db, scripts_dir, run_script_fn):
        r = run_script_fn("record-skill", {
            "plan_id": seeded_plan["plan_id"],
            "name": "systematic-debugging",
            "source": "deferred-load",
            "reason": "test failure surfaced; activating debug skill",
        }, tmp_db)
        assert r.returncode == 0, r.stderr
        conn = sqlite3.connect(str(tmp_db))
        n = conn.execute(
            "SELECT COUNT(*) FROM SkillActivations WHERE plan_id=? AND skill_name=?",
            (seeded_plan["plan_id"], "systematic-debugging"),
        ).fetchone()[0]
        assert n == 1
        conn.close()

    def test_invalid_source(self, seeded_plan, tmp_db, scripts_dir, run_script_fn):
        r = run_script_fn("record-skill", {
            "plan_id": seeded_plan["plan_id"],
            "name": "some-skill",
            "source": "bogus-not-in-enum",
        }, tmp_db)
        assert r.returncode != 0
        msg = (r.stderr + r.stdout).lower()
        assert "source" in msg


# ── 7. finish-plan ─────────────────────────────────────────────────────────

class TestFinishPlan:
    def test_happy_path(self, seeded_plan, tmp_db, scripts_dir, run_script_fn):
        # Complete all steps so finish-plan is legal
        for sid in seeded_plan["step_ids"]:
            run_script_fn("start-step", {"step_id": sid}, tmp_db)
            run_script_fn("complete-step", {"step_id": sid}, tmp_db)
        r = run_script_fn("finish-plan", {"plan_id": seeded_plan["plan_id"]}, tmp_db)
        assert r.returncode == 0, r.stderr
        conn = sqlite3.connect(str(tmp_db))
        status = conn.execute("SELECT status FROM Plans WHERE plan_id=?",
                              (seeded_plan["plan_id"],)).fetchone()[0]
        assert status == "COMPLETED"
        conn.close()

    def test_missing_plan_id(self, tmp_db, scripts_dir, run_script_fn):
        r = run_script_fn("finish-plan", {}, tmp_db)
        assert r.returncode != 0
        assert "plan_id" in (r.stderr + r.stdout).lower()


# ── Cross-cutting: invalid JSON input rejected by every script ─────────────

class TestInputValidation:
    def test_invalid_json_rejected(self, tmp_db, scripts_dir, tmp_path, run_script_fn):
        """Any script given non-JSON input should exit non-zero."""
        bad = tmp_path / "bad.json"
        bad.write_text("this is not json {[")
        import os, subprocess
        env = os.environ.copy()
        env["ORCH_DB"] = str(tmp_db)
        for name in ["start-step", "complete-step", "fail-step", "append-log",
                     "deviate", "record-skill", "finish-plan"]:
            r = subprocess.run(
                ["bash", str(scripts_dir / f"{name}.sh"), str(bad)],
                capture_output=True, text=True, env=env, timeout=10,
            )
            assert r.returncode != 0, f"{name} should reject invalid JSON"
