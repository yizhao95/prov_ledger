"""Tests for ledger_cli.py — provLedger Phase E manual decision-memory CLI.

Mirrors test_publish_plan's subprocess+ORCH_DB pattern: each test gets an
ephemeral migrated DB and invokes the CLI as a real process.
"""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

SKILL_DIR = Path(__file__).resolve().parent.parent
SCRIPTS = SKILL_DIR / "scripts"
ORCH_ROOT = Path.home() / "skill-workspace" / "orchestrator"
sys.path.insert(0, str(ORCH_ROOT))
from orchestrator import db as orch_db  # noqa: E402

PYBIN = ORCH_ROOT / ".venv" / "bin" / "python"


@pytest.fixture
def orch_db_path(tmp_path):
    p = tmp_path / "orch.db"
    c = sqlite3.connect(str(p))
    orch_db.run_migrations(c)
    c.close()
    return p


def _run(args, db_path):
    env = os.environ.copy()
    env["ORCH_DB"] = str(db_path)
    return subprocess.run(
        [str(PYBIN), str(SCRIPTS / "ledger_cli.py"), *args],
        capture_output=True, text=True, env=env, timeout=20)


def test_add_decision(orch_db_path):
    res = _run([
        "add", "--project", "proj", "--kind", "decision",
        "--statement", "rolling-window split, not random",
        "--rationale", "random split leaks temporal info",
        "--subjects", "train_test_split,split",
        "--keywords", "split,rolling,temporal",
    ], orch_db_path)
    assert res.returncode == 0, res.stderr
    c = sqlite3.connect(str(orch_db_path))
    row = c.execute("SELECT project, kind, statement FROM LedgerEntries").fetchone()
    c.close()
    assert row == ("proj", "decision", "rolling-window split, not random")


def test_list_shows_active(orch_db_path):
    _run(["add", "--project", "proj", "--kind", "anti_pattern",
          "--statement", "UPC BIGINT to STRING", "--rationale", "joins broke",
          "--subjects", "upc", "--keywords", "upc,bigint,string"], orch_db_path)
    res = _run(["list", "--project", "proj"], orch_db_path)
    assert res.returncode == 0, res.stderr
    assert "UPC BIGINT to STRING" in res.stdout


def test_invalid_kind_nonzero(orch_db_path):
    res = _run(["add", "--project", "proj", "--kind", "bogus",
                "--statement", "s", "--rationale", "r"], orch_db_path)
    assert res.returncode != 0
