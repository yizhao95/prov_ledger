"""Shared fixtures for executing-plans script tests.

Each test gets:
  - tmp_db: ephemeral SQLite (NOT the real ~/skill-workspace/orchestrator.db)
  - seeded_plan: tmp_db pre-populated with a 3-step plan via the writing-plans
    publish-plan.sh — so we test against REAL plans created by the documented flow

NEVER touches the real orchestrator.db. Lesson learned the hard way last turn.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

# Prefer the repo-bundled orchestrator-backend/ so a fresh public clone is
# self-contained; fall back to the author's internal workspace path otherwise.
_BUNDLED_ORCH = Path(__file__).resolve().parents[3] / "orchestrator-backend"
_DEV_ORCH = Path.home() / "skill-workspace" / "orchestrator"
ORCH_ROOT = _BUNDLED_ORCH if (_BUNDLED_ORCH / "orchestrator" / "__init__.py").exists() else _DEV_ORCH
sys.path.insert(0, str(ORCH_ROOT))

from orchestrator import db as orch_db  # noqa: E402

SKILL_DIR = Path(__file__).parent.parent
SCRIPTS_DIR = SKILL_DIR / "scripts"
# Resolve writing-plans relative to executing-plans so we work in BOTH layouts:
#   install layout: ~/.code_puppy/skills/executing-plans/  -> sibling writing-plans/
#   source-repo:    ~/.code_puppy/skills/skills/executing-plans/  -> sibling writing-plans/
WRITING_PLANS_PUBLISH = SKILL_DIR.parent / "writing-plans" / "scripts" / "publish-plan.sh"


@pytest.fixture
def tmp_db(tmp_path: Path) -> Path:
    """Fresh migrated SQLite DB. Path returned for ORCH_DB env var."""
    db_path = tmp_path / "exec_tests.db"
    conn = sqlite3.connect(str(db_path))
    orch_db.run_migrations(conn)
    conn.close()
    return db_path


@pytest.fixture
def scripts_dir() -> Path:
    return SCRIPTS_DIR


@pytest.fixture
def seeded_plan(tmp_db: Path, tmp_path: Path) -> dict:
    """Publish a plan via the REAL writing-plans flow → returns {plan_id, step_ids}."""
    input_path = tmp_path / "seed.json"
    input_path.write_text(json.dumps({
        "goal": "seed plan for executing-plans tests",
        "prefix": "seed",
        "max_revisions": 5,
        "skills": [
            {"name": "writing-plans", "source": "iron-law"},
            {"name": "executing-plans", "source": "iron-law"},
        ],
        "steps": [
            {"description": "TEST: dummy step A"},
            {"description": "CODE: dummy step B"},
            {"description": "COMMAND: dummy step C"},
        ],
    }))
    env = os.environ.copy()
    env["ORCH_DB"] = str(tmp_db)
    result = subprocess.run(
        ["bash", str(WRITING_PLANS_PUBLISH), str(input_path)],
        capture_output=True, text=True, env=env, timeout=15,
    )
    assert result.returncode == 0, f"seed publish failed: {result.stderr}"
    return json.loads(result.stdout)


def run_script(scripts_dir: Path, script_name: str, input_obj: dict, db_path: Path, env_extra: dict | None = None) -> subprocess.CompletedProcess:
    """Run scripts/<name>.sh against ORCH_DB=db_path with input_obj as JSON via tmp file.

    env_extra: optional extra environment variables (e.g. PSG_REGISTRY_PATH for
    registry isolation in NEEDS_REVIEW tests).

    Returns the CompletedProcess so tests inspect returncode + stdout + stderr.
    """
    import tempfile
    env = os.environ.copy()
    env["ORCH_DB"] = str(db_path)
    if env_extra:
        env.update(env_extra)
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(input_obj, f)
        input_path = f.name
    try:
        return subprocess.run(
            ["bash", str(scripts_dir / f"{script_name}.sh"), input_path],
            capture_output=True, text=True, env=env, timeout=15,
        )
    finally:
        os.unlink(input_path)


@pytest.fixture
def run_script_fn(scripts_dir: Path):
    """Bound runner: tests call run_script_fn('start-step', {...}, tmp_db)."""
    def _run(script_name: str, input_obj: dict, db_path: Path, env_extra: dict | None = None) -> subprocess.CompletedProcess:
        return run_script(scripts_dir, script_name, input_obj, db_path, env_extra=env_extra)
    return _run
