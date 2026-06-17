"""Tests for scripts/publish-plan.sh — the writing-plans → SQLite handoff.

Behavior under test
-------------------
publish-plan.sh accepts ONE positional argument (path to a .json or .yaml
plan-input file). It validates the input, calls orchestrator-cli.py
init-plan with the right flags, and prints the init-plan JSON response
to stdout. Non-zero exit on any validation failure.

Test matrix (5 cases — one per scenario the user spec'd):
  test_valid_json_publishes        valid input → exit 0, prints JSON with plan_id
  test_valid_yaml_publishes        same fields in YAML → exit 0, equivalent result
  test_missing_goal_rejected       missing required field → exit ≠ 0, error mentions 'goal'
  test_invalid_skill_source        bogus source enum → exit ≠ 0, error mentions valid choices
  test_empty_steps_rejected        steps=[] → exit ≠ 0, error mentions 'steps'
  test_publishes_independently     same input twice → 2 distinct plan_ids in DB

Each test uses an ephemeral DB (conftest.tmp_db) and passes its path via
ORCH_DB env var so the script never touches the real database.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from pathlib import Path

import pytest
import yaml


# ── Helpers ────────────────────────────────────────────────────────────

def _valid_input_dict() -> dict:
    """Minimal-but-realistic plan-input that should publish cleanly."""
    return {
        "goal": "smoke goal for tests",
        "prefix": "smoke-test",
        "max_revisions": 3,
        "user_query": "what the user verbatim asked",
        "skills": [
            {"name": "writing-plans", "source": "iron-law"},
            {"name": "test-driven-development", "source": "iron-law"},
        ],
        "steps": [
            {"description": "TEST: write failing test for X", "type": "CODE"},
            {"description": "CODE: implement X", "type": "CODE"},
            {"description": "COMMAND: run pytest", "type": "COMMAND"},
        ],
    }


def _run_publish(scripts_dir: Path, input_path: Path, db_path: Path) -> subprocess.CompletedProcess:
    """Invoke publish-plan.sh with ORCH_DB pointing at the ephemeral DB."""
    env = os.environ.copy()
    env["ORCH_DB"] = str(db_path)
    return subprocess.run(
        ["bash", str(scripts_dir / "publish-plan.sh"), str(input_path)],
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
    )


# ── Tests ──────────────────────────────────────────────────────────────

def test_valid_json_publishes(tmp_path: Path, tmp_db: Path, scripts_dir: Path):
    """Happy path JSON: script exits 0, stdout is JSON with a plan_id and skills_recorded."""
    input_path = tmp_path / "input.json"
    input_path.write_text(json.dumps(_valid_input_dict()))
    result = _run_publish(scripts_dir, input_path, tmp_db)
    assert result.returncode == 0, f"stderr: {result.stderr!r}"
    payload = json.loads(result.stdout)
    assert payload["plan_id"].startswith("smoke-test-")
    assert len(payload["step_ids"]) == 3
    assert set(payload["skills_recorded"]) == {"writing-plans", "test-driven-development"}
    # And the row really landed in the ephemeral DB
    conn = sqlite3.connect(str(tmp_db))
    n = conn.execute("SELECT COUNT(*) FROM Plans WHERE plan_id = ?", (payload["plan_id"],)).fetchone()[0]
    assert n == 1
    n_skills = conn.execute(
        "SELECT COUNT(*) FROM SkillActivations WHERE plan_id = ?", (payload["plan_id"],)
    ).fetchone()[0]
    assert n_skills == 2
    conn.close()


def test_valid_yaml_publishes(tmp_path: Path, tmp_db: Path, scripts_dir: Path):
    """YAML input with same fields publishes equivalently."""
    input_path = tmp_path / "input.yaml"
    input_path.write_text(yaml.safe_dump(_valid_input_dict()))
    result = _run_publish(scripts_dir, input_path, tmp_db)
    assert result.returncode == 0, f"stderr: {result.stderr!r}"
    payload = json.loads(result.stdout)
    assert payload["plan_id"].startswith("smoke-test-")
    assert len(payload["step_ids"]) == 3


def test_missing_goal_rejected(tmp_path: Path, tmp_db: Path, scripts_dir: Path):
    """Required field 'goal' missing → non-zero exit + error mentions 'goal'."""
    bad = _valid_input_dict()
    del bad["goal"]
    input_path = tmp_path / "input.json"
    input_path.write_text(json.dumps(bad))
    result = _run_publish(scripts_dir, input_path, tmp_db)
    assert result.returncode != 0
    assert "goal" in (result.stderr + result.stdout).lower()


def test_invalid_skill_source(tmp_path: Path, tmp_db: Path, scripts_dir: Path):
    """Skill with bogus source enum → non-zero exit + error names valid choices."""
    bad = _valid_input_dict()
    bad["skills"][0]["source"] = "bogus-not-in-enum"
    input_path = tmp_path / "input.json"
    input_path.write_text(json.dumps(bad))
    result = _run_publish(scripts_dir, input_path, tmp_db)
    assert result.returncode != 0
    msg = (result.stderr + result.stdout).lower()
    assert "source" in msg
    # Error should hint at the valid enum so user can self-correct
    assert "iron-law" in msg or "auto-search" in msg


def test_empty_steps_rejected(tmp_path: Path, tmp_db: Path, scripts_dir: Path):
    """steps=[] is invalid (a plan with zero steps is meaningless)."""
    bad = _valid_input_dict()
    bad["steps"] = []
    input_path = tmp_path / "input.json"
    input_path.write_text(json.dumps(bad))
    result = _run_publish(scripts_dir, input_path, tmp_db)
    assert result.returncode != 0
    assert "step" in (result.stderr + result.stdout).lower()


def test_publishes_independently(tmp_path: Path, tmp_db: Path, scripts_dir: Path):
    """Same input twice yields TWO distinct plans (publish is not idempotent — it appends).

    plan_ids embed YYYYMMDDHHMMSS, so we sleep 1.1s between calls to clear the
    timestamp boundary; in real agent usage two consecutive `publish-plan.sh`
    invocations would always be seconds apart.
    """
    import time
    input_path = tmp_path / "input.json"
    input_path.write_text(json.dumps(_valid_input_dict()))
    r1 = _run_publish(scripts_dir, input_path, tmp_db)
    time.sleep(1.1)
    r2 = _run_publish(scripts_dir, input_path, tmp_db)
    assert r1.returncode == 0 and r2.returncode == 0
    p1 = json.loads(r1.stdout)["plan_id"]
    p2 = json.loads(r2.stdout)["plan_id"]
    assert p1 != p2, "two publish calls must create two distinct plans, never overwrite"
    conn = sqlite3.connect(str(tmp_db))
    n = conn.execute("SELECT COUNT(*) FROM Plans").fetchone()[0]
    assert n == 2
    conn.close()


def test_unknown_extension_rejected(tmp_path: Path, tmp_db: Path, scripts_dir: Path):
    """Defensive: only .json/.yaml/.yml accepted (not .txt, etc.)."""
    input_path = tmp_path / "input.txt"
    input_path.write_text(json.dumps(_valid_input_dict()))
    result = _run_publish(scripts_dir, input_path, tmp_db)
    assert result.returncode != 0
    assert "json" in (result.stderr + result.stdout).lower() or "yaml" in (result.stderr + result.stdout).lower()


# ── Phase D: plan-time pre-flight enforcement ────────────────────────────────────

import json as _json
import sqlite3 as _sqlite


def _seed_project_graph(db_path: Path) -> None:
    """Minimal state-graph: alpha->process calls; process produces dv consumed by
    consumer; sql_table sales.daily {cols unknown} read by load_sales."""
    c = _sqlite.connect(str(db_path))
    c.executescript(
        """
        CREATE TABLE node_type (id INTEGER PRIMARY KEY, name TEXT UNIQUE);
        CREATE TABLE node (id INTEGER PRIMARY KEY, node_type_id INTEGER, name TEXT,
                           qualified_name TEXT, file_path TEXT, metadata_json TEXT);
        CREATE TABLE edge_type (id INTEGER PRIMARY KEY, name TEXT UNIQUE);
        CREATE TABLE edge (id INTEGER PRIMARY KEY, edge_type_id INTEGER,
                           src_node_id INTEGER, dst_node_id INTEGER, metadata_json TEXT);
        INSERT INTO node_type (name) VALUES ('function'),('data_var'),('sql_table');
        INSERT INTO edge_type (name) VALUES ('calls'),('produces'),('consumes'),('reads_sql');
        INSERT INTO node (node_type_id,name,qualified_name,file_path,metadata_json) VALUES
          (1,'alpha','pipeline.main','pipeline.py',NULL),
          (1,'process','pipeline.process','pipeline.py',NULL),
          (1,'consumer','mod.consumer','mod.py',NULL),
          (2,'process:return','process:return','pipeline.py','{"dtype":"int"}'),
          (1,'load_sales','loader.load_sales','loader.py',NULL),
          (3,'sales.daily','sales.daily',NULL,'{"assumed_schema":{"store_id":"unknown","amount":"unknown"}}');
        INSERT INTO edge (edge_type_id,src_node_id,dst_node_id,metadata_json) VALUES
          (1,1,2,NULL),          -- alpha calls process
          (2,2,4,'{"type":"int"}'),  -- process produces dv
          (3,4,3,'{"type":"int"}'),  -- dv consumed by consumer
          (4,5,6,NULL);          -- load_sales reads sales.daily
        """
    )
    c.commit()
    c.close()


def _registry(tmp_path: Path, name: str, db_path: Path) -> Path:
    reg = tmp_path / "projects.json"
    reg.write_text(_json.dumps({"projects": [
        {"name": name, "repo": "/x", "db_path": str(db_path), "commit_sha": "abc"}]}))
    return reg


def _run_publish_env(scripts_dir: Path, input_path: Path, db_path: Path,
                     registry: Path) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["ORCH_DB"] = str(db_path)
    env["PSG_REGISTRY_PATH"] = str(registry)
    return subprocess.run(
        ["bash", str(scripts_dir / "publish-plan.sh"), str(input_path)],
        capture_output=True, text=True, env=env, timeout=20)


def _impact_context(db_path: Path) -> dict:
    c = _sqlite.connect(str(db_path)); c.row_factory = _sqlite.Row
    try:
        row = c.execute(
            "SELECT impact_context FROM Plans ORDER BY created_at DESC, rowid DESC LIMIT 1"
        ).fetchone()
    finally:
        c.close()
    return _json.loads(row["impact_context"]) if row and row["impact_context"] else {}


def test_project_plan_without_declared_targets_rejected(tmp_path, tmp_db, scripts_dir):
    gdb = tmp_path / "proj.db"; _seed_project_graph(gdb)
    reg = _registry(tmp_path, "demoproj", gdb)
    plan = _valid_input_dict()
    plan["project"] = "demoproj"          # project named, declared_targets MISSING
    p = tmp_path / "in.json"; p.write_text(_json.dumps(plan))
    res = _run_publish_env(scripts_dir, p, tmp_db, reg)
    assert res.returncode != 0
    assert "declared_targets" in (res.stderr + res.stdout)


def test_project_plan_with_targets_persists_impact_context(tmp_path, tmp_db, scripts_dir):
    gdb = tmp_path / "proj.db"; _seed_project_graph(gdb)
    reg = _registry(tmp_path, "demoproj", gdb)
    plan = _valid_input_dict()
    plan["project"] = "demoproj"
    plan["declared_targets"] = ["pipeline.process"]
    p = tmp_path / "in.json"; p.write_text(_json.dumps(plan))
    res = _run_publish_env(scripts_dir, p, tmp_db, reg)
    assert res.returncode == 0, res.stderr
    ic = _impact_context(tmp_db)
    proc = next(s for s in ic["symbols"] if s["name"] == "pipeline.process")
    assert proc["status"] == "existing"
    assert "pipeline.main" in proc["callers"]      # the real downstream caller


def test_project_plan_unknown_symbol_flagged_new(tmp_path, tmp_db, scripts_dir):
    gdb = tmp_path / "proj.db"; _seed_project_graph(gdb)
    reg = _registry(tmp_path, "demoproj", gdb)
    plan = _valid_input_dict()
    plan["project"] = "demoproj"
    plan["declared_targets"] = ["compute_rolling_window"]
    p = tmp_path / "in.json"; p.write_text(_json.dumps(plan))
    res = _run_publish_env(scripts_dir, p, tmp_db, reg)
    assert res.returncode == 0, res.stderr
    ic = _impact_context(tmp_db)
    sym = next(s for s in ic["symbols"] if s["name"] == "compute_rolling_window")
    assert sym["status"] == "new"


def test_project_plan_surfaces_upstream_assumption(tmp_path, tmp_db, scripts_dir):
    gdb = tmp_path / "proj.db"; _seed_project_graph(gdb)
    reg = _registry(tmp_path, "demoproj", gdb)
    plan = _valid_input_dict()
    plan["project"] = "demoproj"
    plan["declared_targets"] = ["loader.load_sales"]
    p = tmp_path / "in.json"; p.write_text(_json.dumps(plan))
    res = _run_publish_env(scripts_dir, p, tmp_db, reg)
    assert res.returncode == 0, res.stderr
    ic = _impact_context(tmp_db)
    tables = {u["table"] for u in ic["upstream_assumptions"]}
    assert "sales.daily" in tables


def test_projectless_plan_still_publishes(tmp_path, tmp_db, scripts_dir):
    # backward-compat: no 'project' -> no declared_targets needed
    plan = _valid_input_dict()
    p = tmp_path / "in.json"; p.write_text(_json.dumps(plan))
    res = _run_publish_env(scripts_dir, p, tmp_db, tmp_path / "nonexistent.json")
    assert res.returncode == 0, res.stderr
