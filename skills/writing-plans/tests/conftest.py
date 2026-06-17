"""Shared fixtures for writing-plans script tests.

Each test gets its OWN ephemeral SQLite DB so we never touch the real
~/skill-workspace/orchestrator.db. Migrations are applied against the
ephemeral DB so the schema (Plans / Steps / SkillActivations) is real.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

# Allow importing the orchestrator package directly (it's not pip-installed,
# just sitting on disk). Path: ~/skill-workspace/orchestrator/orchestrator/
ORCH_ROOT = Path.home() / "skill-workspace" / "orchestrator"
sys.path.insert(0, str(ORCH_ROOT))

from orchestrator import db as orch_db  # noqa: E402

SKILL_DIR = Path(__file__).parent.parent
SCRIPTS_DIR = SKILL_DIR / "scripts"


@pytest.fixture
def tmp_db(tmp_path: Path) -> Path:
    """Fresh SQLite DB with all migrations applied. Path returned for CLI use."""
    db_path = tmp_path / "test_orchestrator.db"
    conn = sqlite3.connect(str(db_path))
    orch_db.run_migrations(conn)
    conn.close()
    return db_path


@pytest.fixture
def scripts_dir() -> Path:
    """Path to writing-plans/scripts/ — used to invoke publish-plan.sh."""
    return SCRIPTS_DIR
