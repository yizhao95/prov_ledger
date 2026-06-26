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
    r = api.initialize_plan(conn, "Refactor the demo pipeline",
                            ["step one", "step two", "step three"])
    s0, s1, s2 = r["step_ids"]
    api.start_step(conn, s0)
    api.complete_step(conn, s0)
    api.start_step(conn, s1)
    api.fail_step(conn, s1, reason="upstream schema changed")
    api.start_step(conn, s2)
    # a deviation so the revision-history panel (UX4) has something to show
    api.evaluate_and_update_plan(
        conn, deviation_detected=True, target_step_id=s2,
        justification="switch to rolling-window split to avoid temporal leakage",
        new_sub_steps=["use TimeSeriesSplit"])
    conn.close()  # s2 stays IN_PROGRESS -> drives the "Running now" banner (UX2)
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


def test_current_activity_banner(client):
    # DASH-UX2: the running step is surfaced in a "Running now" banner.
    r = client.get("/api/dashboard")
    assert r.status_code == 200
    assert "Running now" in r.text


def test_revision_history_panel(client):
    # DASH-UX4: deviation justification (why the plan changed) is rendered.
    r = client.get("/api/dashboard")
    assert r.status_code == 200
    assert "Revision history" in r.text
    assert "rolling-window split" in r.text


def test_relative_time_helper():
    # DASH-UX5: pure helper — recent past renders as "ago", junk is passed through.
    from datetime import datetime, timezone, timedelta
    from app import queries
    past = (datetime.now(timezone.utc) - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
    assert queries.relative_time(past).endswith("ago")
    assert queries.relative_time(None) == "—"
    assert queries.relative_time("not-a-date") == "not-a-date"


def test_db_is_opened_read_only(client):
    # Read-only invariant: the dashboard connection must reject writes.
    from app import queries
    conn = queries.open_db_readonly(client._db)
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("INSERT INTO Plans (plan_id, original_goal) VALUES ('x', 'y')")
    conn.close()
