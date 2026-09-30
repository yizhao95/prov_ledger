"""The §20 overhead budgets, on a fixture, in an ordinary test run (A5).

H1 and H4 are `live`: they read ~/skill-workspace/orchestrator.db, so they are
deselected by default and a regression in overhead is invisible to CI. Only the
budget CONSTANTS were checked in isolation (test_live_marker.py) — never the
reading path that turns a ledger into a verdict.

So: a fixture ledger of five completed plans with recorded metrics, driven
through the SAME reader the two live tests use (plan_metrics.overhead_report).
Not `over_budget` on a hand-written dict — the path that opens a ledger, picks
the recent completed plans, computes each plan's overhead and says which budget
each one breaks, which plans measured nothing, and which plan got slower.
"""
from __future__ import annotations

import json

import pytest

from orchestrator import db, plan_metrics

REPO_DIR = "/repo"


def _spec(pid, *, steps=10, plain=14, prov=2, orch=4, pack=1000, measured=True):
    """One plan of the fixture. plain/prov/orch are tool_call_log rows inside the
    plan's window; `pack` is the stored context pack's approx_tokens."""
    return {"id": pid, "steps": steps, "plain": plain, "prov": prov, "orch": orch,
            "pack": pack, "measured": measured}


# Five plans, every one comfortably inside all three budgets:
# provenance 2/20 = 0.10, overhead 6/20 = 0.30, context 1000 tokens, 2.0 calls/step.
INSIDE = [_spec(f"P{i}") for i in range(1, 6)]


def _build(conn, specs) -> None:
    for i, s in enumerate(specs):
        day = f"2026-09-{10 + i:02d}"
        created, completed = f"{day} 10:00:00", f"{day} 11:00:00"
        ic = json.dumps({"pack": {"approx_tokens": s["pack"], "shown": 1, "targets": []}}) if s["pack"] else None
        conn.execute("INSERT INTO Plans (plan_id, original_goal, status, project, project_source, created_at, completed_at, impact_context) "
                     "VALUES (?, 'g', 'COMPLETED', 'proj', 'declared', ?, ?, ?)", (s["id"], created, completed, ic))
        for k in range(s["steps"]):
            conn.execute("INSERT INTO Steps (step_id, plan_id, description, status, execution_order, depth_level, log_context, step_type) "
                         "VALUES (?, ?, 'd', 'COMPLETED', ?, 0, '', 'COMMAND')", (f"{s['id']}-{k}", s["id"], k))
        heads = (["bash scripts/reason-fill.sh r"] * s["prov"]
                 + ["bash scripts/run-step.sh a"] * s["orch"]
                 + ["pytest -q"] * s["plain"]) if s["measured"] else []
        for k, head in enumerate(heads):
            conn.execute("INSERT INTO tool_call_log (session_id, cwd, tool_name, command_head, at) VALUES ('s', ?, 'Bash', ?, ?)",
                         (REPO_DIR, head, f"{day} 10:{k:02d}:00"))
    conn.commit()


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    """(path, conn) of a migrated fixture ledger; `repo_for` points at the repo the
    fixture's tool calls were made in, as it does for a real registered project."""
    monkeypatch.setattr(plan_metrics.psg_bridge, "repo_for", lambda p: REPO_DIR)
    path = tmp_path / "fixture.db"
    conn = db.open_db(path)
    db.run_migrations(conn)
    yield path, conn
    conn.close()


def _report(source, **kw):
    return plan_metrics.overhead_report(source, last=5, **kw)


# ── (a) inside every budget ──────────────────────────────────────────────────

def test_a_fixture_inside_every_budget_reports_no_breach(ledger):
    path, conn = ledger
    _build(conn, INSIDE)
    report = _report(path)
    assert report["n_plans"] == 5 and report["n_measured"] == 5, report
    assert report["breaches"] == {}, report["breaches"]
    assert report["gaps"] == [] and report["exceedances"] == [], report
    assert report["budgets"] == plan_metrics.BUDGETS
    row = report["rows"][0]
    assert row["provenance_ratio"] == 0.1 and row["overhead_ratio"] == 0.3 and row["context_overhead_tokens"] == 1000


def test_the_reader_takes_a_path_or_an_open_connection(ledger):
    """H1 hands it a path; H4 keeps the connection because it re-reads the packs."""
    path, conn = ledger
    _build(conn, INSIDE)
    by_path, by_conn = _report(path), _report(conn)
    assert by_path["breaches"] == by_conn["breaches"]
    assert [r["plan_id"] for r in by_path["rows"]] == [r["plan_id"] for r in by_conn["rows"]]
    assert tuple(conn.execute("SELECT 1").fetchone()) == (1,), "an open connection handed in must stay open"


# ── (b) the provenance ratio ─────────────────────────────────────────────────

def test_provenance_ratio_over_the_budget_is_named_and_alone(ledger):
    path, conn = ledger
    specs = list(INSIDE)
    specs[2] = _spec("P3", steps=10, plain=7, prov=2, orch=1)      # prov 2/10 = 0.20, overhead 0.30
    _build(conn, specs)
    report = _report(path)
    assert report["breaches"] == {"P3": ["provenance_ratio"]}, report["breaches"]


# ── (c) the context budget ───────────────────────────────────────────────────

def test_context_overhead_tokens_over_the_budget_is_named(ledger):
    path, conn = ledger
    specs = list(INSIDE)
    specs[1] = _spec("P2", pack=4000)
    _build(conn, specs)
    report = _report(path)
    assert report["breaches"] == {"P2": ["context_overhead_tokens"]}, report["breaches"]
    assert next(r for r in report["rows"] if r["plan_id"] == "P2")["context_overhead_tokens"] == 4000


# ── (d) nothing measured is a gap, not a breach ──────────────────────────────

def test_a_plan_with_nothing_measured_is_a_gap_not_a_breach(ledger):
    path, conn = ledger
    specs = list(INSIDE)
    specs[4] = _spec("P5", measured=False, pack=0)
    _build(conn, specs)
    report = _report(path)
    assert report["gaps"] == ["P5"], report["gaps"]
    assert "P5" not in report["breaches"] and report["breaches"] == {}, report["breaches"]
    assert report["n_measured"] == 4
    assert next(r for r in report["rows"] if r["plan_id"] == "P5")["overhead_ratio"] is None


# ── (e) getting slower is caught against the fixture's own p90 ───────────────

def test_calls_per_step_above_the_factor_of_the_fixtures_own_p90_is_caught(ledger):
    path, conn = ledger
    specs = list(INSIDE)
    specs[4] = _spec("P5", steps=2, plain=30, prov=0, orch=0)      # 15.0 calls/step vs 2.0 elsewhere
    _build(conn, specs)
    report = _report(path)
    # the p90 is the fixture's own, outlier included: 9.8, so the line is 14.7
    assert report["calls_per_step_p90"] == plan_metrics._p90([2.0, 2.0, 2.0, 2.0, 15.0]) == 9.8
    assert report["factor"] == plan_metrics.H1_FACTOR == 1.5
    assert [p for p, _ in report["exceedances"]] == ["P5"], report["exceedances"]
    assert report["exceedances"][0][1] == 15.0
    assert report["breaches"] == {}, "a plan can get slower without breaking a ratio budget"


def test_the_exceedance_verdict_is_none_when_no_plan_was_measured(ledger):
    """"we cannot tell" is a third answer, and it is not "pass"."""
    path, conn = ledger
    _build(conn, [_spec(f"P{i}", measured=False, pack=0) for i in range(1, 6)])
    report = _report(path)
    assert report["calls_per_step_p90"] is None and report["exceedances"] is None
    assert sorted(report["gaps"]) == [f"P{i}" for i in range(1, 6)] and report["breaches"] == {}
