"""Shared pytest fixtures."""
import sqlite3
from pathlib import Path

import pytest

from orchestrator import db


def pytest_collection_modifyitems(config, items):
    """Tests marked r0_r5 stay in their files but do not run (FL-243)."""
    skip = pytest.mark.skip(reason="FL-243: R0-R5 have no product caller since task reasons; see orchestrator/triggers.py")
    for item in items:
        if "r0_r5" in item.keywords:
            item.add_marker(skip)


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    """Fresh in-tmpdir SQLite connection with migrations applied."""
    c = db.open_db(tmp_path / "test.db")
    db.run_migrations(c)
    yield c
    c.close()
