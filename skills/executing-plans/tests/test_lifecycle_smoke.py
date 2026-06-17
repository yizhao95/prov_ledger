"""Full lifecycle smoke test for the deterministic-flow contract.

THE proof for the user's requirement: 'all DB writes are deterministic flows
that the agent just calls'. We pretend to be an agent walking through a real
plan from publish to finish, using ONLY the documented scripts. No direct CLI
calls, no raw SQL writes — just the 8 scripted entry points (1 from
writing-plans + 7 from executing-plans).

Sequence (matches the writing-plans + executing-plans documented flows):
  1. writing-plans/scripts/publish-plan.sh   — create plan
  2. executing-plans/scripts/start-step.sh    — A
  3. executing-plans/scripts/append-log.sh    — A
  4. executing-plans/scripts/complete-step.sh — A
  5. executing-plans/scripts/start-step.sh    — B
  6. executing-plans/scripts/deviate.sh       — B (insert 2 sub-steps)
  7. executing-plans/scripts/start+complete   — B.1, B.2
  8. executing-plans/scripts/complete-step.sh — B
  9. executing-plans/scripts/record-skill.sh  — deferred-load mid-flight
 10. executing-plans/scripts/start+complete   — C
 11. executing-plans/scripts/finish-plan.sh   — plan COMPLETED

Final assertions match the user's verification criteria:
  - plan.status = 'COMPLETED'
  - all steps COMPLETED
  - revision_count = 1 (from the deviate)
  - SkillActivations has 5 init-time + 1 deferred-load = 6 rows
  - append-log content present in step A
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

# Resolve script paths relative to this test file so we work in BOTH layouts:
#   install layout: ~/.code_puppy/skills/executing-plans/tests/  -> sibling writing-plans/
#   source-repo:    ~/.code_puppy/skills/skills/executing-plans/tests/  -> sibling writing-plans/
_SKILL_DIR = Path(__file__).parent.parent
WRITING_PLANS = _SKILL_DIR.parent / "writing-plans" / "scripts"
EXEC = _SKILL_DIR / "scripts"


def _run(script: Path, input_obj: dict, db_path: Path, tmp_path: Path) -> dict:
    """Write input to tmp file, run script, parse JSON stdout. Asserts exit 0."""
    f = tmp_path / f"in_{script.stem}_{os.urandom(4).hex()}.json"
    f.write_text(json.dumps(input_obj))
    env = os.environ.copy()
    env["ORCH_DB"] = str(db_path)
    r = subprocess.run(
        ["bash", str(script), str(f)],
        capture_output=True, text=True, env=env, timeout=15,
    )
    assert r.returncode == 0, f"{script.name} failed: stderr={r.stderr!r}"
    if not r.stdout.strip():
        return {}
    # executing-plans scripts now emit a single-line OK marker as the FINAL stdout
    # line (so agents can `| tail -1 | jq .ok`). publish-plan.sh (writing-plans)
    # doesn't emit such a marker yet — it's a single multi-line JSON. Detect by
    # checking if the last line looks like the marker; strip it if so.
    lines = r.stdout.splitlines()
    if lines[-1].startswith('{"ok":'):
        return json.loads("\n".join(lines[:-1]))
    return json.loads(r.stdout)


def test_full_lifecycle(tmp_path):
    """End-to-end agent simulation against ephemeral DB."""
    db_path = tmp_path / "lifecycle.db"
    # Pre-create + migrate so publish-plan.sh's idempotent migration check skips
    conn = sqlite3.connect(str(db_path))
    orch_db.run_migrations(conn)
    conn.close()

    # ── 1. publish plan via writing-plans ──────────────────────────────
    plan = _run(WRITING_PLANS / "publish-plan.sh", {
        "goal": "lifecycle smoke",
        "prefix": "lc",
        "max_revisions": 5,
        "skills": [
            {"name": "writing-plans",                  "source": "iron-law"},
            {"name": "executing-plans",                "source": "iron-law"},
            {"name": "test-driven-development",        "source": "iron-law"},
            {"name": "verification-before-completion", "source": "iron-law"},
            {"name": "smoke-skill-x",                  "source": "auto-search"},
        ],
        "steps": [
            "ANALYSIS: dummy A",
            "CODE: dummy B",
            "COMMAND: dummy C",
        ],
    }, db_path, tmp_path)
    pid = plan["plan_id"]
    sa, sb, sc = plan["step_ids"]

    # ── 2-4. step A: start, log, complete ──────────────────────────────
    _run(EXEC / "start-step.sh",     {"step_id": sa, "type": "ANALYSIS"}, db_path, tmp_path)
    _run(EXEC / "append-log.sh",     {"step_id": sa, "text": "doing the analysis thing"}, db_path, tmp_path)
    _run(EXEC / "complete-step.sh",  {"step_id": sa, "summary": "analysis done"}, db_path, tmp_path)

    # ── 5-8. step B: start, deviate (2 sub-steps), complete subs + parent ─
    _run(EXEC / "start-step.sh", {"step_id": sb, "type": "CODE"}, db_path, tmp_path)
    dev = _run(EXEC / "deviate.sh", {
        "parent_step_id": sb,
        "justification": "B turned out to need decomposition",
        "sub_steps": ["TEST: write failing test for B sub-feature", "CODE: implement B sub-feature"],
    }, db_path, tmp_path)
    sb1, sb2 = dev["new_step_ids"]
    for s in [sb1, sb2]:
        _run(EXEC / "start-step.sh",    {"step_id": s, "type": "CODE"}, db_path, tmp_path)
        _run(EXEC / "complete-step.sh", {"step_id": s, "summary": f"{s} done"}, db_path, tmp_path)
    _run(EXEC / "complete-step.sh", {"step_id": sb, "summary": "B done after decomposition"}, db_path, tmp_path)

    # ── 9. record a deferred-load skill mid-flight ─────────────────────
    _run(EXEC / "record-skill.sh", {
        "plan_id": pid,
        "name": "systematic-debugging",
        "source": "deferred-load",
        "step_id": sb,
        "reason": "test failure surfaced; activated debug skill",
    }, db_path, tmp_path)

    # ── 10. step C: start + complete ───────────────────────────────────
    _run(EXEC / "start-step.sh",    {"step_id": sc, "type": "COMMAND"}, db_path, tmp_path)
    _run(EXEC / "complete-step.sh", {"step_id": sc, "summary": "C done"}, db_path, tmp_path)

    # ── 11. finish the plan ────────────────────────────────────────────
    _run(EXEC / "finish-plan.sh", {"plan_id": pid}, db_path, tmp_path)

    # ── Final state assertions ─────────────────────────────────────────
    conn = sqlite3.connect(str(db_path))
    plan_row = conn.execute("SELECT status, revision_count FROM Plans WHERE plan_id=?", (pid,)).fetchone()
    assert plan_row[0] == "COMPLETED", f"plan should be COMPLETED, got {plan_row[0]}"
    assert plan_row[1] == 1, f"revision_count should be 1 from the deviate, got {plan_row[1]}"

    # All 6 steps (A, B, B.1, B.2, C, REVIEW) are COMPLETED.
    # The REVIEW step was auto-appended at publish-plan time per migration 006;
    # auto-trigger from complete-step flipped it to COMPLETED when the last
    # non-review step finished. The explicit finish-plan call above is now
    # idempotent (no-op) because the plan was already COMPLETED by the auto-trigger.
    step_statuses = conn.execute(
        "SELECT step_id, status, is_review FROM Steps WHERE plan_id=? ORDER BY execution_order", (pid,)
    ).fetchall()
    assert len(step_statuses) == 6, f"expected 6 steps (5 + REVIEW), got {len(step_statuses)}: {step_statuses}"
    for sid, status, _ in step_statuses:
        assert status == "COMPLETED", f"step {sid} not completed: {status}"
    # Exactly one is_review row, named with the -REVIEW suffix
    review_rows = [r for r in step_statuses if r[2] == 1]
    assert len(review_rows) == 1, f"expected exactly 1 review step, got {len(review_rows)}"
    assert review_rows[0][0] == f"{pid}-REVIEW"

    # Skills: 5 init-time + 1 deferred = 6 rows
    skill_rows = conn.execute(
        "SELECT skill_name, source FROM SkillActivations WHERE plan_id=?", (pid,)
    ).fetchall()
    assert len(skill_rows) == 6, f"expected 6 skill activations, got {len(skill_rows)}: {skill_rows}"
    sources = {row[1] for row in skill_rows}
    assert "iron-law" in sources
    assert "auto-search" in sources
    assert "deferred-load" in sources

    # Append-log content present on step A
    log_a = conn.execute("SELECT log_context FROM Steps WHERE step_id=?", (sa,)).fetchone()[0]
    assert "doing the analysis thing" in log_a

    conn.close()
