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


def test_declared_step_types_persist(tmp_path: Path, tmp_db: Path, scripts_dir: Path):
    """A 'type' declared on a step object lands on the Step row — it used to be
    silently dropped by publish (dashboard showed every step 'untyped')."""
    input_path = tmp_path / "input.json"
    input_path.write_text(json.dumps(_valid_input_dict()))
    result = _run_publish(scripts_dir, input_path, tmp_db)
    assert result.returncode == 0, f"stderr: {result.stderr!r}"
    payload = json.loads(result.stdout)
    conn = sqlite3.connect(str(tmp_db))
    types = [r[0] for r in conn.execute(
        "SELECT step_type FROM Steps WHERE plan_id = ? ORDER BY execution_order",
        (payload["plan_id"],))]
    conn.close()
    # the 3 declared steps + the auto-appended REVIEW marker step (untyped)
    assert types[:3] == ["CODE", "CODE", "COMMAND"]


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


def test_fl020_publish_succeeds_while_graph_locked(tmp_path, tmp_db, scripts_dir):
    """A review refresh holds the graph's write lock while a plan is published:
    the plan must still land, with a visibly degraded impact_context."""
    gdb = tmp_path / "proj.db"; _seed_project_graph(gdb)
    reg = _registry(tmp_path, "demoproj", gdb)
    plan = _valid_input_dict()
    plan["project"] = "demoproj"; plan["declared_targets"] = ["pipeline.process"]
    p = tmp_path / "in.json"; p.write_text(_json.dumps(plan))
    holder = _sqlite.connect(str(gdb)); holder.execute("BEGIN EXCLUSIVE")
    try:
        env_extra = {"PROVLEDGER_GRAPH_BUSY_TIMEOUT_MS": "100"}
        env = os.environ.copy(); env.update(env_extra)
        env["ORCH_DB"] = str(tmp_db); env["PSG_REGISTRY_PATH"] = str(reg)
        res = subprocess.run(["bash", str(scripts_dir / "publish-plan.sh"), str(p)],
                             capture_output=True, text=True, env=env, timeout=30)
    finally:
        holder.rollback(); holder.close()
    assert res.returncode == 0, res.stderr
    assert "graph busy" in (res.stdout + res.stderr)
    ctx = _impact_context(tmp_db)
    assert ctx.get("degraded") == "graph busy"


# ── Task 14: expectations captured at publish ────────────────────────────────

def _project_plan(tmp_path, tmp_db, scripts_dir, expectations, with_project=True):
    gdb = tmp_path / "proj.db"
    if not gdb.exists():
        _seed_project_graph(gdb)
    reg = _registry(tmp_path, "demoproj", gdb)
    plan = _valid_input_dict()
    if with_project:
        plan["project"] = "demoproj"; plan["declared_targets"] = ["pipeline.process"]
    plan["expectations"] = expectations
    p = tmp_path / "in.json"; p.write_text(_json.dumps(plan))
    return _run_publish_env(scripts_dir, p, tmp_db, reg)


def test_expectations_are_recorded_at_publish(tmp_path, tmp_db, scripts_dir):
    res = _project_plan(tmp_path, tmp_db, scripts_dir, [
        {"target": "pipeline.process", "target_kind": "node", "claim": "process keeps its output shape", "channel": "graph"},
        {"target": "orders", "target_kind": "dataset", "claim": "promo_discount stays present", "channel": "profile_drift"}])
    assert res.returncode == 0, res.stderr
    c = _sqlite.connect(str(tmp_db))
    rows = c.execute("SELECT plan_id, project, target, target_kind, channel FROM expectations ORDER BY id").fetchall()
    assert len(rows) == 2 and rows[0][1] == "demoproj" and rows[0][2:] == ("pipeline.process", "node", "graph")
    assert rows[1][2:] == ("orders", "dataset", "profile_drift")
    assert rows[0][0] == c.execute("SELECT plan_id FROM Plans ORDER BY rowid DESC LIMIT 1").fetchone()[0]
    assert '"expectations_recorded": 2' in res.stdout


def test_no_expectations_no_rows(tmp_path, tmp_db, scripts_dir):
    res = _project_plan(tmp_path, tmp_db, scripts_dir, [])
    assert res.returncode == 0, res.stderr
    assert _sqlite.connect(str(tmp_db)).execute("SELECT COUNT(*) FROM expectations").fetchone()[0] == 0


def test_invalid_expectation_is_rejected(tmp_path, tmp_db, scripts_dir):
    for bad in ({"target": "x", "target_kind": "node", "claim": "c", "channel": "telepathy"},
                {"target": "x", "target_kind": "table", "claim": "c", "channel": "none"},
                {"target": "x", "target_kind": "node", "channel": "graph"}):
        res = _project_plan(tmp_path, tmp_db, scripts_dir, [bad])
        assert res.returncode != 0 and "expectations" in (res.stderr + res.stdout)
    assert _sqlite.connect(str(tmp_db)).execute("SELECT COUNT(*) FROM Plans").fetchone()[0] == 0


def test_expectations_require_a_project(tmp_path, tmp_db, scripts_dir):
    res = _project_plan(tmp_path, tmp_db, scripts_dir,
                        [{"target": "x", "target_kind": "node", "claim": "c", "channel": "graph"}], with_project=False)
    assert res.returncode != 0 and "project" in (res.stderr + res.stdout)


# ── FL-021: publish migrates a legacy DB on open ─────────────────────────────

def test_fl021_publish_migrates_a_legacy_db(tmp_path, scripts_dir):
    from orchestrator import db as orch_db
    legacy = tmp_path / "legacy.db"
    c = orch_db.open_db(legacy)
    for f in sorted(orch_db.MIGRATIONS_DIR.glob("*.sql"))[:13]:
        c.executescript(f.read_text())
        c.execute("INSERT OR IGNORE INTO schema_version (version) VALUES ((SELECT COALESCE(MAX(version), 0) + 1 FROM schema_version))")
    c.commit(); c.close()
    p = tmp_path / "in.json"; p.write_text(_json.dumps(_valid_input_dict()))
    res = _run_publish(scripts_dir, p, legacy)
    assert res.returncode == 0, res.stderr
    cols = {row[1] for row in _sqlite.connect(str(legacy)).execute("PRAGMA table_info(Plans)")}
    assert {"project", "project_source", "impact_context"} <= cols
    assert _sqlite.connect(str(legacy)).execute("SELECT COUNT(*) FROM Plans").fetchone()[0] == 1


# ── FL-014 / E5-3: attribution is explicit — declared or derived from cwd ────

def _git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "-C", str(path), "init", "-q"], check=True)
    (path / "README.md").write_text("x\n")
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(path), "-c", "user.email=a@b.c", "-c", "user.name=t", "commit", "-qm", "init"], check=True)
    return path


def _registry_repos(tmp_path: Path, entries: dict) -> Path:
    """entries: name -> repo path; each gets a seeded graph."""
    projects = []
    for name, repo in entries.items():
        gdb = tmp_path / f"{name}.db"
        if not gdb.exists():
            _seed_project_graph(gdb)
        projects.append({"name": name, "repo": str(repo), "db_path": str(gdb), "commit_sha": "abc"})
    reg = tmp_path / "projects.json"; reg.write_text(_json.dumps({"projects": projects}))
    return reg


def _publish_from(scripts_dir: Path, plan: dict, db_path: Path, registry: Path, cwd: Path, tmp_path: Path):
    p = tmp_path / "in.json"; p.write_text(_json.dumps(plan))
    env = os.environ.copy(); env["ORCH_DB"] = str(db_path); env["PSG_REGISTRY_PATH"] = str(registry)
    return subprocess.run(["bash", str(scripts_dir / "publish-plan.sh"), str(p)],
                          capture_output=True, text=True, env=env, timeout=30, cwd=str(cwd))


def _latest_plan(db_path: Path) -> dict:
    c = _sqlite.connect(str(db_path)); c.row_factory = _sqlite.Row
    return dict(c.execute("SELECT * FROM Plans ORDER BY rowid DESC LIMIT 1").fetchone())


def test_e5_3a_cwd_inside_registered_repo_attributes_automatically(tmp_path, tmp_db, scripts_dir):
    repo = _git_repo(tmp_path / "repo")
    reg = _registry_repos(tmp_path, {"myproj": repo})
    plan = _valid_input_dict(); plan["declared_targets"] = ["pipeline.process"]   # no 'project' key, goal says nothing
    (repo / "sub").mkdir()
    r = _publish_from(scripts_dir, plan, tmp_db, reg, cwd=repo / "sub", tmp_path=tmp_path)   # a subdirectory of the repo
    assert r.returncode == 0, r.stderr
    p = _latest_plan(tmp_db)
    assert (p["project"], p["project_source"]) == ("myproj", "cwd")
    assert '"project_source": "cwd"' in r.stdout
    assert p["impact_context"] is not None                                        # preflight ran for the derived project


def test_e5_3b_project_none_is_recorded_and_announced(tmp_path, tmp_db, scripts_dir):
    repo = _git_repo(tmp_path / "repo")
    reg = _registry_repos(tmp_path, {"myproj": repo})
    plan = _valid_input_dict(); plan["project"] = "none"
    r = _publish_from(scripts_dir, plan, tmp_db, reg, cwd=repo, tmp_path=tmp_path)
    assert r.returncode == 0, r.stderr
    p = _latest_plan(tmp_db)
    assert (p["project"], p["project_source"]) == ("none", "declared")
    assert "no state-graph review" in (r.stdout + r.stderr)


def test_e5_3c_conflict_between_cwd_and_declared_fails_publish(tmp_path, tmp_db, scripts_dir):
    repo = _git_repo(tmp_path / "repo"); other = _git_repo(tmp_path / "other")
    reg = _registry_repos(tmp_path, {"myproj": repo, "other": other})
    plan = _valid_input_dict(); plan["project"] = "other"; plan["declared_targets"] = ["pipeline.process"]
    r = _publish_from(scripts_dir, plan, tmp_db, reg, cwd=repo, tmp_path=tmp_path)
    assert r.returncode != 0 and "pick one" in (r.stderr + r.stdout)
    assert _sqlite.connect(str(tmp_db)).execute("SELECT COUNT(*) FROM Plans").fetchone()[0] == 0


def test_declared_matching_cwd_is_fine(tmp_path, tmp_db, scripts_dir):
    repo = _git_repo(tmp_path / "repo")
    reg = _registry_repos(tmp_path, {"myproj": repo})
    plan = _valid_input_dict(); plan["project"] = "myproj"; plan["declared_targets"] = ["pipeline.process"]
    r = _publish_from(scripts_dir, plan, tmp_db, reg, cwd=repo, tmp_path=tmp_path)
    assert r.returncode == 0, r.stderr
    assert (_latest_plan(tmp_db)["project"], _latest_plan(tmp_db)["project_source"]) == ("myproj", "declared")


def test_no_repo_no_project_publishes_with_warning(tmp_path, tmp_db, scripts_dir):
    reg = _registry_repos(tmp_path, {"myproj": _git_repo(tmp_path / "repo")})
    plan = _valid_input_dict()
    r = _publish_from(scripts_dir, plan, tmp_db, reg, cwd=tmp_path, tmp_path=tmp_path)   # tmp_path is not a git repo
    assert r.returncode == 0, r.stderr
    p = _latest_plan(tmp_db)
    assert p["project"] is None and p["project_source"] is None
    assert "not attributed" in (r.stdout + r.stderr)


def test_cwd_in_unregistered_nested_repo_is_not_attributed(tmp_path, tmp_db, scripts_dir):
    repo = _git_repo(tmp_path / "repo"); inner = _git_repo(repo / "vendor" / "inner")
    reg = _registry_repos(tmp_path, {"myproj": repo})
    r = _publish_from(scripts_dir, _valid_input_dict(), tmp_db, reg, cwd=inner, tmp_path=tmp_path)
    assert r.returncode == 0, r.stderr
    assert _latest_plan(tmp_db)["project"] is None            # the inner repo is what git sees; it is not registered
