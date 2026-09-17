"""Route-layer tests for the dashboard (DASH-TEST1).

Exercises the FastAPI routes against an ephemeral migrated DB via TestClient,
covering the happy path, the failed-step surfacing (DASH-UX3), the plan-not-found
branch (DASH-BUG1 — no leaked handle / no 500), and read-only enforcement.
"""
from __future__ import annotations

import importlib
import json
import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
ORCH_BACKEND = REPO / "orchestrator-backend"
sys.path.insert(0, str(ORCH_BACKEND))

from orchestrator import api, db as odb  # noqa: E402


def vocab_ui(key):
    from app import vocab
    return vocab.ui(key)


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

def _reclass(db_path):
    """Run the legacy-reason migration on a test DB whose rows were inserted after it first opened."""
    c = odb.open_db(db_path)
    c.execute("DELETE FROM migration_state WHERE key='dp_reclass'"); c.commit(); c.close()
    odb.open_db(db_path).close()


def _insert_reasons(db_path, plan_id):
    """One row per tier: two legacy node_reason rows migrated (derived constraint_ref,
    asserted rejected_path), then a stated reason pointing at an utterance and an
    unstated backstop through the new store."""
    from orchestrator import provenance as pv
    conn = odb.open_db(db_path)
    odb.insert_node_reason(conn, node_key="nk_obs", project="demo", run_id=1, plan_id=plan_id, kind="constraint_ref",
                           text="constraint_bypassed:1: exclude region X", source="system", tier="derived")
    odb.insert_node_reason(conn, node_key="nk_ast", project="demo", run_id=1, plan_id=plan_id, kind="rejected_path",
                           text="TimeSeriesSplit leaked future rows", source="agent", tier="asserted")
    conn.close()
    _reclass(db_path)
    conn = odb.open_db(db_path)
    u = pv.insert_utterance(conn, session_id="s", project="demo", plan_id=plan_id, text="fiscal weeks", occurred_at="2026-09-15 10:00:00")
    pv.insert_reason(conn, project="demo", plan_id=plan_id, node_key="nk_sta", kind="technical", run_id=1, verbatim=(u, 0, 12), recorded_by="agent")
    pv.insert_reason(conn, project="demo", plan_id=plan_id, node_key="nk_uns", kind="technical", run_id=1, recorded_by="system")
    conn.close()


def test_e3_1_tier_badges_distinct_without_color(client):
    _insert_reasons(client._db, client._seeded["plan_id"])
    r = client.get("/api/dashboard")
    assert r.status_code == 200 and vocab_ui("reasons_panel") in r.text
    for tier in ("derived", "asserted", "stated", "unstated"):
        assert f'data-tier="{tier}"' in r.text and f">{tier}<" in r.text, tier
    assert "— unstated —" in r.text and "nk_uns" in r.text and 'data-source-level="verbal"' in r.text
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


# ── Phase 8 Task 4 (FL-009): /node/{project}/{qualified_name} ───────────────
sys.path.insert(0, str(ORCH_BACKEND / "tests"))
import _psg_schema as ps  # noqa: E402


def _seed_state_graph(tmp_path, project="demo"):
    """A minimal PSG-shaped graph + registry: nk_a added in run 1 (plan P0), renamed
    and changed in run 2 (plan P1) with an asserted identity event; a card with
    callers/consumers; two constraints on it (one restricted)."""
    path = tmp_path / f"{project}-state-graph.db"
    c = ps.build(path)
    ps.add_run(c, 1, plan_id="P0", step_id="P0-A", sha="aaaaaaa")
    ps.add_snapshot(c, 1, "nk_a", "pkg.m.load")
    ps.add_event(c, 1, 1, "node_added", "nk_a", json.dumps({"qualified_name": "pkg.m.load"}))
    ps.add_run(c, 2, plan_id="P1", step_id="P1-REVIEW.1", sha="bbbbbbb")
    ps.add_snapshot(c, 2, "nk_a", "pkg.m.load_orders", struct_sig="s2")
    ps.add_event(c, 2, 1, "node_matched", "nk_a", '{"via": "struct_sig"}')
    ps.add_event(c, 2, 2, "node_renamed", "nk_a", '{"from": "pkg.m.load", "to": "pkg.m.load_orders"}')
    ps.add_event(c, 2, 3, "node_changed", "nk_a", '{"changed": ["struct_sig"]}')
    c.execute("INSERT INTO node_event (run_id, seq, event_type, node_key, tier, payload_json, created_at) VALUES (2, 4, 'identity_asserted', 'nk_a', 'asserted', ?, '2026-09-11T00:00:00+00:00')",
              (json.dumps({"cur": "pkg.m.load_orders", "evidence": "main() now calls load_orders (line 12)", "arbiter": "anthropic.claude_headless"}),))
    c.executescript("""
        CREATE TABLE IF NOT EXISTS consistency_card (symbol_id INTEGER PRIMARY KEY, card_json TEXT NOT NULL);
        INSERT INTO node_type (id, name) VALUES (1, 'function');
        INSERT INTO node (id, node_type_id, name, qualified_name, file_path, run_id, node_key) VALUES (7, 1, 'load_orders', 'pkg.m.load_orders', 'pkg/m.py', 2, 'nk_a');
        INSERT INTO consistency_card (symbol_id, card_json) VALUES (7, '{"callers": ["pkg.m.main"], "callees": ["pd.read_csv"], "output_consumers": ["pkg.m.clean"], "reads": ["orders"], "writes": []}');
    """)
    c.commit(); c.close()
    reg = tmp_path / "projects.json"
    reg.write_text(json.dumps({"projects": [{"name": project, "repo": str(tmp_path), "db_path": str(path), "commit_sha": "bbbbbbb"}]}))
    return path, reg


def _seed_reasons_and_constraints(db, project="demo"):
    """Through the DP store: a stated reason (utterance span), an unstated backstop,
    and two constraints (one restricted) mirrored the way ledger_store.add_entry does."""
    from orchestrator import constraints as oc, provenance as pv
    conn = odb.open_db(db)
    u = pv.insert_utterance(conn, session_id="s", project=project, plan_id="P1",
                            text="renamed so the name says which table it reads", occurred_at="2026-09-15 10:00:00")
    pv.insert_reason(conn, project=project, plan_id="P1", node_key="nk_a", kind="technical", run_id=2, step_id="P1-REVIEW.1",
                     verbatim=(u, 0, len("renamed so the name says which table it reads")), recorded_by="agent")
    pv.insert_reason(conn, project=project, plan_id="P1", node_key="nk_a", kind="technical", run_id=2, recorded_by="system")
    oc.record_constraint(conn, project=project, subjects=["pkg.m.load_orders"], statement="load_orders must keep paid orders only",
                         rationale="finance reconciles on paid orders", why_ref="docs/finance.md#paid", why_visibility="shared")
    oc.record_constraint(conn, project=project, subjects=["nk_a"], statement="never read the raw orders table in prod",
                         rationale="SECRET-RATIONALE-DO-NOT-SHOW", why_ref="ticket SEC-42", why_visibility="restricted")
    conn.commit(); conn.close()


def test_node_page_shows_space_time_and_reasons_in_one_query(client, tmp_path, monkeypatch):
    _, reg = _seed_state_graph(tmp_path)
    monkeypatch.setenv("PSG_REGISTRY_PATH", str(reg))
    _seed_reasons_and_constraints(client._db)
    # DP 2d (Task 3b): the default timeline carries only the moments that changed
    # something, so the full event list now lives behind ?show=all — the quiet
    # events are folded with a count, never dropped.
    r = client.get("/node/demo/pkg.m.load_orders?show=all")
    assert r.status_code == 200
    t = r.text
    # time: every event on one rail, each with its plan link and tier label
    for ev in ("node_added", "node_matched", "node_renamed", "node_changed", "identity_asserted"):
        assert ev in t
    # DP 2d: the rail's change rows are one line — the arbiter's evidence rides in
    # the row rather than as a second paragraph; the tier attribute is unchanged
    assert 'data-tier="asserted"' in t
    # DP 2d (Task 3b + layout spec 6): a link back to a task now carries the whole
    # triple and the step anchor, so it lands on the moment rather than the page top
    assert 'href="/plan/P0?node=pkg.m.load_orders' in t and 'href="/plan/P1?node=pkg.m.load_orders' in t
    # space: the card
    assert "pkg.m.main" in t and "pkg.m.clean" in t
    # reasons + constraints; the restricted rationale never leaves the ledger
    assert "renamed so the name says which table it reads" in t and 'data-tier="stated"' in t and 'data-tier="unstated"' in t
    assert "load_orders must keep paid orders only" in t and "finance reconciles on paid orders" in t
    assert "never read the raw orders table in prod" in t and "ticket SEC-42" in t and "SECRET-RATIONALE" not in t
    assert vocab_ui("context_estimate") in t
    from app import queries
    conn = odb.open_db(client._db)
    ledger = queries.get_node_ledger(conn, "demo", "pkg.m.load_orders")
    conn.close()
    assert ledger["available"] and ledger["node_key"] == "nk_a" and [g["run_id"] for g in ledger["runs"]] == [1, 2]
    assert ledger["card"]["callers"] == ["pkg.m.main"] and len(ledger["reasons"]) == 2 and len(ledger["constraints"]) == 2
    assert ledger["constraints"][1]["rationale"] is None and ledger["approx_tokens"] > 0


def test_node_page_accepts_a_node_key_and_the_reasons_panel_links_to_it(client, tmp_path, monkeypatch):
    _, reg = _seed_state_graph(tmp_path)
    monkeypatch.setenv("PSG_REGISTRY_PATH", str(reg))
    _seed_reasons_and_constraints(client._db)
    r = client.get("/node/demo/nk_a")
    assert r.status_code == 200 and "pkg.m.load_orders" in r.text and "node_renamed" in r.text
    # the plan page's Reasons panel links every node_key to its ledger page
    conn = odb.open_db(client._db)
    conn.execute("UPDATE Plans SET project='demo' WHERE plan_id=?", (client._seeded["plan_id"],))
    from orchestrator import provenance as pv
    pv.insert_reason(conn, project="demo", plan_id=client._seeded["plan_id"], node_key="nk_a", kind="technical", run_id=2,
                     interpretation="linked", recorded_by="agent")
    conn.commit(); conn.close()
    r = client.get("/")
    assert 'href="/node/demo/nk_a"' in r.text


def test_node_page_says_unavailable_when_the_graph_is_missing(client, tmp_path, monkeypatch):
    monkeypatch.setenv("PSG_REGISTRY_PATH", str(tmp_path / "no-registry.json"))
    r = client.get("/node/demo/pkg.m.load_orders")
    assert r.status_code == 200 and "state graph unavailable" in r.text
    _, reg = _seed_state_graph(tmp_path)
    monkeypatch.setenv("PSG_REGISTRY_PATH", str(reg))
    r = client.get("/node/demo/pkg.m.never_seen")
    assert r.status_code == 200 and "not in the state graph" in r.text


# ── DP phase 1 Task 7: the Reasons panels show the source level ─────────────
def test_reasons_panel_shows_source_level_for_migrated_and_new_rows(client):
    pid = client._seeded["plan_id"]
    conn = odb.open_db(client._db)
    conn.execute("UPDATE Plans SET project='demo' WHERE plan_id=?", (pid,))
    odb.insert_node_reason(conn, node_key="nk_old", project="demo", run_id=1, plan_id=pid, kind="reason",
                           text="legacy sentence written as stated", source="agent", tier="stated")
    conn.execute("DELETE FROM migration_state WHERE key='dp_reclass'")
    conn.commit(); conn.close()
    conn = odb.open_db(client._db)                       # the reclass runs on open
    from orchestrator import provenance as pv
    u = pv.insert_utterance(conn, session_id="s", project="demo", plan_id=pid, text="keep paid orders only", occurred_at="2026-09-15 10:00:00")
    pv.insert_reason(conn, project="demo", plan_id=pid, node_key="nk_new", kind="technical", verbatim=(u, 0, 21), recorded_by="agent")
    pv.insert_reason(conn, project="demo", plan_id=pid, node_key="nk_gap", kind="technical", recorded_by="system")
    conn.close()
    r = client.get("/api/dashboard")
    assert r.status_code == 200 and vocab_ui("reasons_panel") in r.text
    # DP 2d (Task 3d): the node page says the level in plain words ("linked"),
    # the plan panels still print the "source level" prefix; both are the same column.
    import re as _re
    assert "linked" in r.text or "verbal" in r.text or "task-context" in r.text
    assert not _re.search(r"[\u4e00-\u9fff]", r.text), "the English page prints a Chinese phrase"
    assert 'data-tier="asserted"' in r.text and "legacy sentence written as stated" in r.text      # migrated: asserted, not stated
    assert 'data-tier="stated"' in r.text and 'data-source-level="verbal"' in r.text
    assert 'data-tier="unstated"' in r.text and 'data-source-level="unstated"' in r.text
    assert 'data-source-level="task_context"' in r.text
    from app import queries
    conn = odb.open_db(client._db)
    rows = {x["node_key"]: x for x in queries.get_node_reasons(conn, pid)}
    conn.close()
    assert rows["nk_old"]["tier"] == "asserted" and rows["nk_old"]["evidence_level"] == "task_context"
    assert rows["nk_new"]["tier"] == "stated" and rows["nk_new"]["source_level"].startswith("source level verbal")
    assert queries.get_unstated(conn if False else odb.open_db(client._db), pid)["unstated"] == 1


def test_node_page_constraints_come_from_change_reason(client, tmp_path, monkeypatch):
    _, reg = _seed_state_graph(tmp_path)
    monkeypatch.setenv("PSG_REGISTRY_PATH", str(reg))
    _seed_reasons_and_constraints(client._db)            # two constraints through the store (one restricted)
    r = client.get("/node/demo/pkg.m.load_orders")
    assert r.status_code == 200
    assert "load_orders must keep paid orders only" in r.text and "finance reconciles on paid orders" in r.text
    assert "never read the raw orders table in prod" in r.text and "ticket SEC-42" in r.text and "SECRET-RATIONALE" not in r.text
    # DP 2d (Task 3d): the node page says how checkable the source is in plain
    # words instead of printing the column name — same column, a reader's phrasing
    assert ("linked" in r.text or "verbal" in r.text or "task-context" in r.text or "unstated" in r.text)
    from app import queries
    conn = odb.open_db(client._db)
    ledger = queries.get_node_ledger(conn, "demo", "pkg.m.load_orders")
    conn.close()
    assert len(ledger["constraints"]) == 2 and {c["why_visibility"] for c in ledger["constraints"]} == {"shared", "restricted"}
    assert all("source_level" in c for c in ledger["constraints"])


# ── DP phase 2 (Task 7): headline block, shown / adopted per step, hit counts per record, overhead ──

def _seed_headline(db, pid, step_id):
    """A headline with one blocking finding answered by the agent (proceed) and one
    unanswered, the records it showed (read_hit) and adopted (influence) — written
    through plain SQL the way checks.headline / respond do."""
    conn = odb.open_db(db)
    conn.execute("INSERT INTO change_reason (project, plan_id, node_key, kind, role, statement, occurred_at, recorded_by, tier, hash) "
                 "VALUES ('demo', 'ledger', 'nk_a', 'organizational', 'constraint', 'load_orders must keep paid orders only', '2026-09-01 00:00:00', 'human', 'asserted', 'h1')")
    cid = conn.execute("SELECT MAX(id) FROM change_reason").fetchone()[0]
    conn.execute("INSERT INTO change_reason (project, plan_id, node_key, kind, role, interpretation, occurred_at, recorded_by, tier, rule_id, hash) "
                 "VALUES ('demo', 'P0', 'nk_a', 'technical', 'rejected_path', 'tried dropping nulls first', '2026-09-02 00:00:00', 'system', 'derived', 'R6', 'h2')")
    rid = conn.execute("SELECT MAX(id) FROM change_reason").fetchone()[0]
    doc = {"findings": [
        {"id": "active_constraint:nk_a:1", "layer": "self", "kind": "active_constraint", "tier": "asserted", "severity": "blocking",
         "text": "load_orders must keep paid orders only (human, 2026-09-01)", "anchor": "pkg.m.load_orders", "evidence": {"reason_id": cid}, "hard": False},
        {"id": "downstream_break:nk_a:1", "layer": "impact", "kind": "downstream_break", "tier": "derived", "severity": "warning",
         "text": "1 consumer(s) eat its output: pkg.m.clean", "anchor": "pkg.m.load_orders", "evidence": {"consumers": ["pkg.m.clean"]}, "hard": False},
        {"id": "active_constraint:nk_a:2", "layer": "self", "kind": "active_constraint", "tier": "stated", "severity": "blocking",
         "text": "never read the raw orders table in prod", "anchor": "pkg.m.load_orders", "evidence": {"reason_id": cid}, "hard": False}],
        "summary": {"targets": 1, "layers": 2, "findings": 3, "blocking": 2, "warning": 1, "info": 0, "unanswered": 2, "shown": 2, "adopted": 0}, "hints": ["1 more reason is not expanded; run `provledger why pkg.m.load_orders --all`"]}
    conn.execute("INSERT INTO headline (project, plan_id, findings_json) VALUES ('demo', ?, ?)", (pid, json.dumps(doc, ensure_ascii=False)))
    hid = conn.execute("SELECT MAX(id) FROM headline").fetchone()[0]
    conn.execute("INSERT INTO headline_response (headline_id, finding_id, action, rationale, by, cites_json) VALUES (?, 'active_constraint:nk_a:1', 'proceed', 'the filter moves downstream', 'agent', '[]')", (hid,))
    conn.execute("INSERT INTO read_hit (reason_id, project, plan_id, moment) VALUES (?, 'demo', ?, 'plan')", (cid, pid))
    conn.execute("INSERT INTO read_hit (reason_id, project, plan_id, moment) VALUES (?, 'demo', ?, 'plan')", (rid, pid))
    conn.execute("INSERT INTO read_hit (reason_id, project, plan_id, step_id, moment, injected_chars) VALUES (?, 'demo', ?, ?, 'edit', 240)", (cid, pid, step_id))
    conn.execute("INSERT INTO influence (reason_id, project, plan_id, step_id, node_key, via, by) VALUES (?, 'demo', ?, ?, 'nk_a', 'headline_response', 'agent')", (cid, pid, step_id))
    conn.execute("UPDATE Plans SET project='demo', project_source='declared' WHERE plan_id=?", (pid,))
    conn.commit(); conn.close()
    return {"cid": cid, "rid": rid, "hid": hid}


def test_headline_block_shows_findings_with_severity_and_the_unanswered_count(client):
    pid = client._seeded["plan_id"]; step = client._seeded["step_ids"][0]
    _seed_headline(client._db, pid, step)
    html = client.get("/api/dashboard").text
    assert 'data-panel="headline"' in html and 'data-unanswered="1"' in html and 'data-findings="3"' in html
    assert html.count('data-severity="blocking"') == 2 and html.count('data-severity="warning"') == 1
    assert 'data-tier="stated"' in html and 'data-agent-proceeded="1"' in html and 'data-unanswered-finding="1"' in html
    assert "→ proceed (agent) · the filter moves downstream" in html and "unanswered" in html and "provledger why pkg.m.load_orders --all" in html
    assert "Surfaced 2" in html and "Adopted 0" in html                                # plan-level buckets (the step's rows are the step's)


def test_step_panel_has_shown_and_adopted_columns(client):
    pid = client._seeded["plan_id"]; step = client._seeded["step_ids"][0]
    ids = _seed_headline(client._db, pid, step)
    html = client.get("/api/dashboard").text
    assert f'data-step-records="{step}"' in html and 'data-shown="1"' in html and 'data-adopted="1"' in html
    assert f'href="/node/demo/nk_a?at={ids["cid"]}"' in html               # adopted entries link to the record
    assert "Surfaced 1" in html and "Adopted 1" in html


def test_node_page_hit_counts_per_moment_and_the_adopting_plan_backlink(client, tmp_path, monkeypatch):
    _, reg = _seed_state_graph(tmp_path)
    monkeypatch.setenv("PSG_REGISTRY_PATH", str(reg))
    pid = client._seeded["plan_id"]; step = client._seeded["step_ids"][0]
    ids = _seed_headline(client._db, pid, step)
    html = client.get("/node/demo/nk_a").text
    # DP 2d (Task 3d): the prose is now plain language; the per-moment counts stay
    # machine-readable in data-* so the ETag, this suite and any scraper are unaffected
    assert f'data-stats="{ids["cid"]}"' in html and 'data-shown-plan="1"' in html and 'data-adopted="1"' in html
    # DP 2d: the link carries the TYPED triple (a record id, not a run); Task 3d
    # replaced "adopted by <plan>" with the sentence a person would say, and moved the
    # plan id into title= (layout spec 8).
    assert f'href="/plan/{pid}?node=pkg.m.load_orders&at=reason:{ids["cid"]}" title="{pid}"' in html
    assert "Adopted by plan" in html
    assert "hits" in html
    hi = client.get(f"/node/demo/nk_a?at=reason:{ids['cid']}").text
    assert hi.count("data-hit-here") == 1, "more than one row is lit"        # exactly one thing lit (DP 2d)


def test_footer_carries_the_two_overhead_numbers(client):
    pid = client._seeded["plan_id"]
    conn = odb.open_db(client._db)
    created = conn.execute("SELECT created_at FROM Plans WHERE plan_id=?", (pid,)).fetchone()[0]
    conn.execute("INSERT INTO tool_call_log (session_id, cwd, tool_name, command_head, at) VALUES ('s', '/x', 'Bash', 'bash scripts/run-step.sh a', ?)", (created,))
    conn.execute("INSERT INTO tool_call_log (session_id, cwd, tool_name, command_head, at) VALUES ('s', '/x', 'Bash', 'pytest -q', ?)", (created,))
    conn.execute("UPDATE Plans SET impact_context=? WHERE plan_id=?", (json.dumps({"pack": {"approx_tokens": 321}}), pid))
    conn.commit(); conn.close()
    html = client.get("/api/dashboard").text
    assert 'data-overhead-ratio="0.5"' in html and 'data-context-overhead-tokens="321"' in html
    assert "overhead_ratio 0.5" in html and "context_overhead_tokens 321" in html


def test_headline_and_records_degrade_to_200_on_an_old_db(client):
    conn = odb.open_db(client._db)
    for t in ("headline_response", "headline", "influence", "read_hit"):
        conn.execute(f"DROP TABLE {t}")
    conn.execute("DROP VIEW reason_stats_v"); conn.commit(); conn.close()
    r = client.get("/api/dashboard")
    assert r.status_code == 200 and 'data-panel="headline"' not in r.text and "data-step-records" not in r.text
    assert client.get("/node/demo/nk_a").status_code == 200


def test_etag_changes_on_a_headline_response(client):
    from app import queries
    pid = client._seeded["plan_id"]; step = client._seeded["step_ids"][0]
    ids = _seed_headline(client._db, pid, step)
    ro = queries.open_db_readonly(client._db); before = queries.compute_etag(ro); ro.close()
    conn = odb.open_db(client._db)
    conn.execute("INSERT INTO headline_response (headline_id, finding_id, action, rationale, by, cites_json) VALUES (?, 'active_constraint:nk_a:2', 'revise', 'ok', 'human', '[]')", (ids["hid"],))
    conn.commit(); conn.close()
    ro = queries.open_db_readonly(client._db); after = queries.compute_etag(ro); ro.close()
    assert before != after


# ── DP phase 2b (Task 3): the Graph view ──

def test_graph_page_renders_nodes_with_badge_and_tier_and_links_to_node(client, tmp_path, monkeypatch):
    _, reg = _seed_state_graph(tmp_path)
    monkeypatch.setenv("PSG_REGISTRY_PATH", str(reg))
    _seed_reasons_and_constraints(client._db)
    html = client.get("/graph/demo?mode=full").text                   # DP 2d: the bare page is the `story` mode now
    assert 'data-panel="graph-table"' in html and 'data-node="nk_a"' in html
    assert 'data-badge="3"' in html                                # 1 stated reason + 2 active constraints (the unstated one has no badge)
    assert 'data-tier="asserted"' in html                          # the latest event of nk_a is identity_asserted
    assert 'href="/node/demo/pkg.m.load_orders"' in html and "run 2" in html and "bbbbbbb" in html
    focused = client.get("/graph/demo?focus=pkg.m.load_orders").text
    assert 'data-focus="1"' in focused and 'name="focus" value="pkg.m.load_orders"' in focused
    assert 'data-mode="focus"' in focused and 'data-mode="story"' in client.get("/graph/demo").text


def test_graph_page_at_a_run_shows_that_runs_nodes_and_says_where_edges_come_from(client, tmp_path, monkeypatch):
    _, reg = _seed_state_graph(tmp_path)
    monkeypatch.setenv("PSG_REGISTRY_PATH", str(reg))
    then = client.get("/graph/demo?at=run:1&mode=full").text
    assert "run 1" in then and "aaaaaaa" in then and "edges_from: latest" in then
    assert 'href="/node/demo/pkg.m.load?at=run:1"' in then              # the name it carried at run 1, and the triple carries a TYPED at
    now = client.get("/graph/demo?mode=full").text
    assert "pkg.m.load_orders" in now and 'href="/node/demo/pkg.m.load?at=run:1"' not in now


def test_graph_page_without_a_graph_is_200_and_says_so(client, tmp_path, monkeypatch):
    monkeypatch.setenv("PSG_REGISTRY_PATH", str(tmp_path / "empty.json")); (tmp_path / "empty.json").write_text('{"projects": []}')
    r = client.get("/graph/nope")
    assert r.status_code == 200 and 'data-state="unavailable"' in r.text and "state graph unavailable" in r.text


def test_graph_page_full_level_includes_data_nodes_and_old_db_stays_200(client, tmp_path, monkeypatch):
    _, reg = _seed_state_graph(tmp_path)
    monkeypatch.setenv("PSG_REGISTRY_PATH", str(reg))
    conn = odb.open_db(client._db); conn.execute("DROP VIEW node_badge_v"); conn.commit(); conn.close()
    r = client.get("/graph/demo?level=full&mode=full")
    assert r.status_code == 200 and 'data-badge="0"' in r.text                 # no badge view → badges read 0, page still renders


# ── DP phase 2b (Task 5): session cards ──

def _seed_session(db, pid, sid="sess-A"):
    conn = odb.open_db(db)
    conn.execute("INSERT INTO utterance (session_id, project, plan_id, text, occurred_at, visibility, hash) VALUES (?, 'demo', ?, 'please keep fiscal weeks', '2026-09-16 09:00:00', 'personal', 'h1')", (sid, pid))
    conn.execute("INSERT INTO utterance (session_id, project, plan_id, text, occurred_at, visibility, hash) VALUES (?, 'demo', NULL, 'and rename load to load_orders', '2026-09-16 09:01:00', 'shareable', 'h2')", (sid,))
    for head, tool in (("bash scripts/run-step.sh a", "Bash"), ("bash scripts/reason-fill.sh r", "Bash"), ("pytest -q", "Bash"), (None, "Edit")):
        conn.execute("INSERT INTO tool_call_log (session_id, cwd, tool_name, command_head) VALUES (?, '/x', ?, ?)", (sid, tool, head))
    conn.execute("UPDATE Plans SET session_id=?, project='demo' WHERE plan_id=?", (sid, pid))
    conn.execute("INSERT INTO session_run (session_id, project, cwd, started_at, ended_at, refresh_state, note) VALUES ('sess-D', 'demo', '/x', '2026-09-16 08:00:00', '2026-09-16 08:30:00', 'done', 'refresh queued: tracked files changed')")
    conn.execute("UPDATE session_run SET psg_run_id = 2 WHERE session_id='sess-D'")
    conn.execute("INSERT INTO utterance (session_id, project, plan_id, text, occurred_at, visibility, hash) VALUES ('sess-D', 'demo', NULL, 'add order_count', '2026-09-16 08:10:00', 'shareable', 'h3')")
    conn.execute("INSERT INTO headline (project, session_id, findings_json) VALUES ('demo', 'sess-D', ?)", (json.dumps({"findings": [], "summary": {"targets": 1, "layers": 2, "findings": 0, "blocking": 0, "warning": 0, "info": 0, "unanswered": 0, "shown": 0, "adopted": 0}}),))
    conn.commit(); conn.close()


def test_session_page_shows_said_cost_changed_and_plans(client, tmp_path, monkeypatch):
    _, reg = _seed_state_graph(tmp_path); monkeypatch.setenv("PSG_REGISTRY_PATH", str(reg))
    pid = client._seeded["plan_id"]; _seed_session(client._db, pid)
    html = client.get("/session/sess-A").text
    assert 'data-session="sess-A"' in html and 'data-degraded="1"' not in html
    assert 'data-visibility="personal"' in html and "please keep fiscal weeks" in html and "and rename load to load_orders" in html
    assert "orchestration 1 (25.0%)" in html and "provenance 1 (25.0%)" in html and "other 2" in html
    assert f'href="/plan/{pid}"' in html and "no plan published" not in html


def test_degraded_session_is_marked_and_shows_its_refresh_and_headline(client, tmp_path, monkeypatch):
    _, reg = _seed_state_graph(tmp_path); monkeypatch.setenv("PSG_REGISTRY_PATH", str(reg))
    pid = client._seeded["plan_id"]; _seed_session(client._db, pid)
    conn = odb.open_db(client._db)
    conn.execute("UPDATE analysis_run SET plan_id='session:sess-D' WHERE id=2") if False else None
    conn.close()
    html = client.get("/session/sess-D").text
    assert 'data-degraded="1"' in html and "degraded (no plan)" in html and "refresh done" in html and "run 2" in html
    assert "data-session-headline" in html and "never printed" in html and "data-no-plan" in html
    assert client.get("/session/nope").status_code == 200 and 'data-state="not-found"' in client.get("/session/nope").text


def test_home_lists_recent_sessions_with_and_without_a_plan(client, tmp_path, monkeypatch):
    _, reg = _seed_state_graph(tmp_path); monkeypatch.setenv("PSG_REGISTRY_PATH", str(reg))
    pid = client._seeded["plan_id"]; _seed_session(client._db, pid)
    html = client.get("/").text
    assert 'data-panel="recent-sessions"' in html and 'data-session-card="sess-A"' in html and 'data-session-card="sess-D" data-degraded="1"' in html
    assert "1 plan" in html and "degraded (no plan)" in html


def test_plan_header_names_its_session_and_the_sessions_other_plans(client):
    pid = client._seeded["plan_id"]; _seed_session(client._db, pid)
    conn = odb.open_db(client._db)
    conn.execute("INSERT INTO Plans (plan_id, original_goal, status, project, project_source, session_id) VALUES ('other-plan', 'g2', 'COMPLETED', 'demo', 'declared', 'sess-A')")
    conn.commit(); conn.close()
    html = client.get(f"/plan/{pid}").text
    assert 'data-plan-session="sess-A"' in html and 'href="/session/sess-A"' in html and 'href="/plan/other-plan"' in html


def test_session_pages_survive_an_old_db(client):
    conn = odb.open_db(client._db)
    for t in ("session_run",):
        conn.execute(f"DROP TABLE {t}")
    conn.commit(); conn.close()
    assert client.get("/session/x").status_code == 200 and client.get("/").status_code == 200
