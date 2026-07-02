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
    # Phase 5.2: data panel rows — written through the backbone (odb), never by
    # the dashboard. One profile snapshot + one drift decision.
    odb.insert_data_profile(conn, [
        {"plan_id": r["plan_id"], "step_id": s0, "dataset": "events",
         "column_name": "label", "dtype": "str", "null_frac": 0.0,
         "row_count": 100, "distinct_count": 1},
        {"plan_id": r["plan_id"], "step_id": s0, "dataset": "events",
         "column_name": "amount", "dtype": "float", "null_frac": 0.5,
         "row_count": 100, "distinct_count": 87},
    ])
    odb.insert_llm_decision(
        conn, decision="halt the run: label dtype flipped int->str upstream",
        plan_id=r["plan_id"], step_id=s0, dataset="events", column="label",
        observed_before="int", observed_after="str", drift_kind="dtype_changed",
        rationale="downstream model would silently predict a constant class",
        action="halt", outcome="halted", failure=True)
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


def test_data_panel_renders(client):
    # Phase 5.2: profile snapshot + drift-decision trail are rendered read-only.
    r = client.get("/api/dashboard")
    assert r.status_code == 200
    assert "📊 Data" in r.text
    # profile table: dataset, columns, dtype
    assert "events" in r.text
    assert "label" in r.text and "amount" in r.text
    # decision trail: drift kind + decision + failure surfaced above the fold
    assert "dtype_changed" in r.text
    assert "label dtype flipped int-&gt;str upstream" in r.text
    assert "1 unresolved" in r.text  # halted counts as unresolved -> panel auto-opens


def test_data_panel_shows_latest_snapshot_only(client, tmp_path):
    # A re-profile (later observed_at) replaces the earlier snapshot per dataset.
    conn = odb.open_db(client._db)
    conn.execute(
        "INSERT INTO data_profile (plan_id, step_id, dataset, column_name, dtype,"
        " null_frac, row_count, distinct_count, observed_at)"
        " VALUES (?, ?, 'events', 'label', 'int', 0.0, 100, 2,"
        " strftime('%Y-%m-%d %H:%M:%S', 'now', '+1 hour'))",
        (client._seeded["plan_id"], client._seeded["step_ids"][2]),
    )
    conn.commit()
    conn.close()
    from app import queries
    ro = queries.open_db_readonly(client._db)
    profiles = queries.get_data_profiles(ro, client._seeded["plan_id"])
    ro.close()
    assert len(profiles) == 1
    p = profiles[0]
    assert p["snapshot_count"] == 2
    cols = {c["column_name"]: c for c in p["columns"]}
    assert set(cols) == {"label"}          # 'amount' was dropped in the re-profile
    assert cols["label"]["dtype"] == "int"  # latest snapshot wins


def test_data_panel_degrades_on_old_db(tmp_path, monkeypatch):
    # A DB predating migrations 012/013 renders fine with no data panel.
    db = tmp_path / "old.db"
    seeded = _seed_db(db)
    conn = sqlite3.connect(db)
    conn.execute("DROP TABLE data_profile")
    conn.execute("DROP TABLE llm_decisions")
    conn.commit()
    conn.close()
    monkeypatch.setenv("ORCH_DB", str(db))
    from app import queries, main
    importlib.reload(queries)
    importlib.reload(main)
    from fastapi.testclient import TestClient
    c = TestClient(main.app)
    r = c.get("/api/dashboard")
    assert r.status_code == 200
    assert "📊 Data" not in r.text
    _ = seeded


def test_etag_changes_on_new_decision(client):
    # The 2s poll must not 304 through a freshly-recorded data decision.
    from app import queries
    ro = queries.open_db_readonly(client._db)
    before = queries.compute_etag(ro)
    ro.close()
    conn = odb.open_db(client._db)
    odb.insert_llm_decision(
        conn, decision="adapt downstream cast", plan_id=client._seeded["plan_id"],
        dataset="events", column="label", drift_kind="dtype_changed",
        action="adapt_downstream", outcome="resolved")
    conn.close()
    ro = queries.open_db_readonly(client._db)
    after = queries.compute_etag(ro)
    ro.close()
    assert before != after


def test_db_is_opened_read_only(client):
    # Read-only invariant: the dashboard connection must reject writes.
    from app import queries
    conn = queries.open_db_readonly(client._db)
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("INSERT INTO Plans (plan_id, original_goal) VALUES ('x', 'y')")
    conn.close()
