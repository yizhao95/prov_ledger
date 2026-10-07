"""A publish that fails leaves nothing behind (FL-216).

publish_plan wrote the plan row first and checked the rest afterwards: an
invalid step type was found only when the step was inserted, and the
expectations and headline-note checks ran after the plan existed. Each left an
IN_PROGRESS plan with no steps that nothing could close. Every check now runs
before the first write, and the plan, its steps and its skills are written in
one transaction.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from pathlib import Path


def _publish(scripts_dir: Path, tmp_path: Path, db_path: Path, data: dict) -> subprocess.CompletedProcess:
    f = tmp_path / "plan.json"
    f.write_text(json.dumps(data))
    env = {**os.environ, "ORCH_DB": str(db_path)}
    return subprocess.run(["bash", str(scripts_dir / "publish-plan.sh"), str(f)], capture_output=True,
                          text=True, env=env, timeout=30, cwd=str(tmp_path))


def _counts(db_path: Path) -> dict:
    c = sqlite3.connect(str(db_path))
    try:
        return {t: c.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in ("Plans", "Steps", "SkillActivations")}
    finally:
        c.close()


BASE = {"goal": "g", "prefix": "atomic", "project": "none",
        "skills": [{"name": "writing-plans", "source": "iron-law"}],
        "steps": [{"description": "TEST: a", "type": "CODE"}, {"description": "CODE: b", "type": "CODE"}]}


def test_an_invalid_step_type_writes_nothing(scripts_dir, tmp_path, tmp_db):
    bad = {**BASE, "steps": [{"description": "TEST: a", "type": "TEST"}]}
    p = _publish(scripts_dir, tmp_path, tmp_db, bad)
    assert p.returncode != 0
    assert "type" in p.stderr and "TEST" in p.stderr
    assert _counts(tmp_db) == {"Plans": 0, "Steps": 0, "SkillActivations": 0}


def test_expectations_without_a_tracked_project_write_nothing(scripts_dir, tmp_path, tmp_db):
    bad = {**BASE, "expectations": [{"target": "x", "target_kind": "node", "claim": "c", "channel": "metric:m", "step": 0}]}
    p = _publish(scripts_dir, tmp_path, tmp_db, bad)
    assert p.returncode != 0 and "expectations" in p.stderr
    assert _counts(tmp_db) == {"Plans": 0, "Steps": 0, "SkillActivations": 0}


def test_a_headline_note_citing_a_record_that_does_not_exist_writes_nothing(scripts_dir, tmp_path, tmp_db):
    bad = {**BASE, "headline_notes": [{"finding_kind": "similar_intent", "cites": [99999], "text": "see this"}]}
    p = _publish(scripts_dir, tmp_path, tmp_db, bad)
    assert p.returncode != 0 and "99999" in p.stderr
    assert _counts(tmp_db) == {"Plans": 0, "Steps": 0, "SkillActivations": 0}


def test_a_valid_plan_writes_plan_steps_and_skills(scripts_dir, tmp_path, tmp_db):
    p = _publish(scripts_dir, tmp_path, tmp_db, BASE)
    assert p.returncode == 0, p.stderr
    n = _counts(tmp_db)
    assert n["Plans"] == 1 and n["Steps"] >= 2 and n["SkillActivations"] == 1
