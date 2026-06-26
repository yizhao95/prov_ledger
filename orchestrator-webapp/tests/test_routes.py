"""Route-layer tests for the dashboard (DASH-TEST1).

Exercises the FastAPI routes against an ephemeral migrated DB via TestClient,
covering the happy path, the failed-step surfacing (DASH-UX3), the plan-not-found
branch (DASH-BUG1 — no leaked handle / no 500), and read-only enforcement.
"""
from __future__ import annotations

import importlib
import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
ORCH_BACKEND = REPO / "orchestrator-backend"
sys.path.insert(0, str(ORCH_BACKEND))

from orchestrator import api, db as odb  # noqa: E402


def _seed_db(path: Path) -> dict:
    conn = odb.open_db(path)
    odb.run_migrations(conn)
    r = api.initialize_plan(conn, "Refactor the demo pipeline", ["step one", "step two"])
    s0, s1 = r["step_ids"]
    api.start_step(conn, s0)
    api.complete_step(conn, s0)
    api.start_step(conn, s1)
    api.fail_step(conn, s1, reason="upstream schema changed")
    conn.close()
    return r


@pytest.fixture
def client(tmp_path, monkeypatch):
    db = tmp_path / "orch.db"
    seeded = _seed_db(db)
    monkeypatch.setenv("ORCH_DB", str(db))
    from app import queries, main
    importlib.reload(queries)
    importlib.reload(main)
    from fastapi.testclient import TestClient
    c = TestClient(main.app)
    c._seeded = seeded
    c._db = db
    return c


def test_health_ok(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_dashboard_renders_plan(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "Refactor the demo pipeline" in r.text


def test_failed_step_surfaces_in_partial(client):
    # DASH-UX3: a failed step must be visible above the fold (chip + red).
    r = client.get("/api/dashboard")
    assert r.status_code == 200
    assert "failed" in r.text.lower()


def test_plan_not_found_does_not_500(client):
    # DASH-BUG1/BUG2: missing plan returns a clean page, not a 500 / leaked handle.
    r = client.get("/plan/does-not-exist")
    assert r.status_code == 200
    assert "Plan not found" in r.text


def test_db_is_opened_read_only(client):
    # Read-only invariant: the dashboard connection must reject writes.
    from app import queries
    conn = queries.open_db_readonly(client._db)
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("INSERT INTO Plans (plan_id, original_goal) VALUES ('x', 'y')")
    conn.close()
