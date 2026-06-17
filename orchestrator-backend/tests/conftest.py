"""Shared pytest fixtures."""
import sqlite3
from pathlib import Path

import pytest

from orchestrator import db


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    """Fresh in-tmpdir SQLite connection with migrations applied."""
    c = db.open_db(tmp_path / "test.db")
    db.run_migrations(c)
    yield c
    c.close()
