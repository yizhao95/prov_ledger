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


# ── 3.7-A: tier badges, reasons panel, unstated tile (E3-1 / E3-2 / E3-3) ──

def _insert_reasons(db_path, plan_id):
    conn = odb.open_db(db_path)
    rows = [("nk_obs", "constraint_ref", "constraint_bypassed:1: exclude region X", "system", "derived"),
            ("nk_ast", "rejected_path", "TimeSeriesSplit leaked future rows", "agent", "asserted"),
            ("nk_sta", "reason", "fiscal weeks", "agent", "stated"),
            ("nk_uns", "reason", None, "system", "derived")]
    for key, kind, text, source, tier in rows:
        odb.insert_node_reason(conn, node_key=key, project="demo", run_id=1, plan_id=plan_id, kind=kind,
                               text=text, source=source, tier=tier)
    conn.close()


def test_e3_1_tier_badges_distinct_without_color(client):
    _insert_reasons(client._db, client._seeded["plan_id"])
    r = client.get("/api/dashboard")
    assert r.status_code == 200 and "🧭 Reasons" in r.text
    for tier in ("derived", "asserted", "stated", "unstated"):
        assert f'data-tier="{tier}"' in r.text and f">{tier}<" in r.text, tier
    assert "— unstated —" in r.text and "fiscal weeks" in r.text and "nk_uns" in r.text
    # observed is a known tier even when no row carries it
    from app import queries
    assert queries.tier_badge("observed")[0] == "observed" and queries.tier_badge(None)[0] == "unstated"
    assert len({queries.tier_badge(t)[0] for t in ("observed", "derived", "asserted", "stated", "unstated")}) == 5


def test_e3_2_failed_step_stays_visible_after_plan_completes(client):
    """The seeded plan has a FAILED step with a recovered deviation; finishing the
    plan must not hide the failure."""
    seeded = client._seeded
    conn = odb.open_db(client._db)
    s1, s2 = seeded["step_ids"][1], seeded["step_ids"][2]
    # recover s1's failure through its sub-step, finish s2, close the plan
    api.evaluate_and_update_plan(conn, deviation_detected=True, target_step_id=s1,
                                 justification="rerun with the new schema", new_sub_steps=["rerun ingest"])
    odb.update_step_status(conn, f"{s1}.1", "COMPLETED", set_completed=True)
    odb.update_step_status(conn, f"{s2}.1", "COMPLETED", set_completed=True)
    api.complete_step(conn, s2)
    odb.insert_review_step(conn, seeded["plan_id"])        # initialize_plan has no review row; publish-plan adds it
    out = api.review_and_complete(conn, seeded["plan_id"], registry_path=str(client._db.parent / "absent.json"))
    assert out["plan_status"] == "COMPLETED", out
    conn.close()
    r = client.get("/api/dashboard")
    assert "COMPLETED" in r.text and "FAILED" in r.text and "never hidden" in r.text
    assert "upstream schema changed" in r.text


def test_e3_3_no_non_get_routes():
    from app import main
    for route in main.app.routes:
        methods = getattr(route, "methods", None) or {"GET"}
        assert set(methods) <= {"GET", "HEAD"}, (route.path, methods)


def test_unstated_tile_and_etag(client):
    r0 = client.get("/api/dashboard")
    etag0 = r0.headers["etag"]
    assert "unstated" in r0.text            # the tile is always there (0 when nothing recorded)
    _insert_reasons(client._db, client._seeded["plan_id"])
    r1 = client.get("/api/dashboard", headers={"If-None-Match": etag0})
    assert r1.status_code == 200 and r1.headers["etag"] != etag0      # a node_reason row invalidates the etag
    assert "1/2" in r1.text or "50%" in r1.text                        # slots 2 (nk_sta, nk_uns), unstated 1
    from app import queries
    conn = odb.open_db(client._db)
    assert queries.get_unstated(conn, client._seeded["plan_id"]) == {"slots": 2, "unstated": 1, "pct": 50}
    rows = queries.get_node_reasons(conn, client._seeded["plan_id"])
    assert [r["display_tier"] for r in rows] == ["derived", "asserted", "stated", "unstated"]
    conn.close()


# ── phase 3.5 Task 6: the plan card shows its project attribution ────────────

def test_plan_card_shows_project_attribution(client):
    conn = odb.open_db(client._db)
    odb.set_plan_project(conn, client._seeded["plan_id"], "demo-app", "declared")
    conn.close()
    r = client.get("/api/dashboard")
    assert r.status_code == 200
    assert 'data-project-source="declared"' in r.text and "demo-app" in r.text
    assert "unattributed" not in r.text


def test_plan_card_shows_unattributed_when_null(client):
    r = client.get("/api/dashboard")
    assert 'data-project-source="none"' in r.text and "unattributed" in r.text


# ── phase 7 Task 1: the plan's outcomes (metric delta) are read-only visible ────

def test_outcomes_panel_shows_metric_delta(client):
    conn = odb.open_db(client._db)
    pid = client._seeded["plan_id"]
    eid = odb.insert_expectation(conn, plan_id=pid, step_id=None, project="demo", target="mean_net_revenue",
                                 target_kind="metric", claim="revenue stays within ±5% WoW", channel="metric:mean_net_revenue")
    odb.insert_outcome(conn, expectation_id=eid, kind="observed",
                       value={"name": "mean_net_revenue", "before": 43.6, "after": 53.72, "delta": 10.12, "delta_pct": 23.2,
                              "unit": "usd", "before_at": "2026-09-11 00:30:00", "after_at": "2026-09-11 02:00:00"},
                       source="metrics", tier="observed", backfilled_by_plan="PU2")
    e2 = odb.insert_expectation(conn, plan_id=pid, step_id=None, project="demo", target="orders", target_kind="dataset",
                                claim="promo_discount stays", channel="profile_drift")
    odb.insert_outcome(conn, expectation_id=e2, kind="none_available", value={}, source="data_profile", tier="none",
                       reason="no data_profile snapshot before and after the expectation", backfilled_by_plan="PU2")
    conn.close()
    r = client.get("/api/dashboard")
    assert r.status_code == 200 and "🎯 Outcomes" in r.text
    assert "+23.2%" in r.text and "43.6" in r.text and "53.72" in r.text and "mean_net_revenue" in r.text
    assert "revenue stays within ±5% WoW" in r.text and 'data-tier="observed"' in r.text
    assert "no data_profile snapshot" in r.text and 'data-tier="none"' in r.text
    from app import queries
    conn = odb.open_db(client._db)
    rows = queries.get_outcomes(conn, pid)
    conn.close()
    assert [x["channel"] for x in rows] == ["metric:mean_net_revenue", "profile_drift"]
    assert rows[0]["delta_pct"] == 23.2 and rows[0]["summary"].startswith("43.6") and rows[1]["delta_pct"] is None


def _seed_expectations(db, pid):
    conn = odb.open_db(db)
    r2 = api.initialize_plan(conn, "Second plan — reprice promo", ["one"], plan_id_prefix="second")
    pid2 = r2["plan_id"]
    e1 = odb.insert_expectation(conn, plan_id=pid, step_id=None, project="demo", target="mean_net_revenue",
                                target_kind="metric", claim="revenue stays within ±5% WoW", channel="metric:mean_net_revenue")
    odb.insert_outcome(conn, expectation_id=e1, kind="observed",
                       value={"name": "mean_net_revenue", "before": 43.6, "after": 53.72, "delta": 10.12, "delta_pct": 23.2, "unit": "usd"},
                       source="metrics", tier="observed", backfilled_by_plan=pid2)
    e2 = odb.insert_expectation(conn, plan_id=pid2, step_id=None, project="other", target="orders", target_kind="dataset",
                                claim="promo_discount column stays", channel="profile_drift")
    odb.insert_outcome(conn, expectation_id=e2, kind="none_available", value={}, source="data_profile", tier="none",
                       reason="no data_profile snapshot before and after the expectation", backfilled_by_plan=pid)
    e3 = odb.insert_expectation(conn, plan_id=pid2, step_id=None, project="other", target="pkg.m.load", target_kind="node",
                                claim="load keeps its callers", channel="survival")
    conn.close()
    return pid2, (e1, e2, e3)


def test_outcomes_page_lists_every_claim_with_its_latest_outcome(client):
    pid = client._seeded["plan_id"]
    pid2, (e1, e2, e3) = _seed_expectations(client._db, pid)
    r = client.get("/outcomes")
    assert r.status_code == 200
    assert "revenue stays within ±5% WoW" in r.text and "promo_discount column stays" in r.text and "load keeps its callers" in r.text
    assert 'data-tier="observed"' in r.text and 'data-tier="none"' in r.text and 'data-tier="pending"' in r.text
    assert "+23.2%" in r.text and "metric:mean_net_revenue" in r.text and "profile_drift" in r.text
    assert f"/plan/{pid2}" in r.text and "Second plan" in r.text     # the owning plan and who backfilled
    for key in ("observed", "none_available", "pending"):                   # the stat strip: one labelled tile per kind
        assert f'data-stat="{key}">1<' in r.text, key
    assert 'data-stat="n">3<' in r.text and 'data-stat="survival">0<' in r.text
    from app import queries
    conn = odb.open_db(client._db)
    rows = queries.get_expectations_with_latest_outcome(conn)
    conn.close()
    assert [x["expectation_id"] for x in rows] == [e3, e2, e1]          # created_at DESC, id DESC
    assert rows[0]["kind"] == "pending" and rows[2]["latest"]["delta_pct"] == 23.2 and rows[2]["plan_title"]


def test_outcomes_page_filters_by_project(client):
    pid = client._seeded["plan_id"]
    _seed_expectations(client._db, pid)
    r = client.get("/outcomes?project=other")
    assert r.status_code == 200 and "promo_discount column stays" in r.text and "revenue stays within" not in r.text
    r = client.get("/outcomes?project=nope")
    assert r.status_code == 200 and "no expectations" in r.text.lower()


def test_outcomes_page_is_empty_but_200_on_an_old_db(tmp_path, monkeypatch):
    db = tmp_path / "old.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE Plans (plan_id TEXT PRIMARY KEY, original_goal TEXT, status TEXT, revision_count INTEGER, "
              "max_revisions INTEGER, created_at TEXT, completed_at TEXT)")
    c.commit(); c.close()
    monkeypatch.setenv("ORCH_DB", str(db))
    from app import queries, main
    importlib.reload(queries); importlib.reload(main)
    from fastapi.testclient import TestClient
    r = TestClient(main.app).get("/outcomes")
    assert r.status_code == 200 and "no expectations" in r.text.lower()
