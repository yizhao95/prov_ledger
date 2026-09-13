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

# Import the orchestrator package directly (it's not pip-installed, just on disk).
# Prefer the repo-bundled orchestrator-backend/ so a fresh public clone is
# self-contained; fall back to the author's internal workspace path otherwise.
_BUNDLED_ORCH = Path(__file__).resolve().parents[3] / "orchestrator-backend"
_DEV_ORCH = Path.home() / "skill-workspace" / "orchestrator"
ORCH_ROOT = _BUNDLED_ORCH if (_BUNDLED_ORCH / "orchestrator" / "__init__.py").exists() else _DEV_ORCH
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


@pytest.fixture(autouse=True)
def _isolated_registry(tmp_path, monkeypatch):
    """FL-014 (phase 3.5): publish derives a plan's project from the repo it runs
    in. These tests run inside prov_ledger — a registered project — so every
    test gets an EMPTY registry unless it points PSG_REGISTRY_PATH elsewhere.
    Tests must never read the real ~/skill-workspace registry."""
    reg = tmp_path / "empty-registry.json"
    reg.write_text('{"projects": []}')
    monkeypatch.setenv("PSG_REGISTRY_PATH", str(reg))
