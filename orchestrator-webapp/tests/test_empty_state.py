"""The empty state names the command that actually creates a plan.

Both empty states told the reader to run `orchestrator-cli.py init-plan`, a
legacy script that a plugin install does not even have on its path. Plans are
published by the writing-plans skill (`publish-plan.sh`); the `provledger` CLI
reads the ledger and has no plan-creating command. The phrase lives in vocab.py,
so it exists in both languages.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
from provledger import db as odb  # conftest binds provledger to this checkout


@pytest.fixture
def client(tmp_path, monkeypatch):
    dbp = tmp_path / "orch.db"
    conn = odb.open_db(dbp)
    odb.run_migrations(conn)
    conn.close()                          # a migrated ledger with no plan in it
    monkeypatch.setenv("ORCH_DB", str(dbp))
    from app import queries, main
    importlib.reload(queries); importlib.reload(main)
    from fastapi.testclient import TestClient
    return TestClient(main.app)


@pytest.mark.parametrize("path", ["/", "/api/dashboard", "/history"])
@pytest.mark.parametrize("lang", ["en", "zh"])
def test_the_empty_state_names_the_real_command(client, path, lang):
    from app import vocab
    t = client.get(f"{path}?lang={lang}").text
    assert "orchestrator-cli" not in t
    assert vocab.ui("no_plans_yet", lang) in t
    assert vocab.PUBLISH_PLAN_CMD in t


def test_the_named_script_exists():
    from app import vocab
    script = vocab.PUBLISH_PLAN_CMD.split()[1]
    assert (REPO / script).is_file(), f"{script} is not in the repository"
