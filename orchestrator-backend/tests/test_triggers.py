"""orchestrator.triggers — the deterministic reason rules R1–R6 (spec §4, C1/C2/C4)."""
import json
import sys
from pathlib import Path

import pytest

from orchestrator import api, db, provenance as pv, triggers

sys.path.insert(0, str(Path(__file__).parent))
import _psg_schema as ps  # noqa: E402

PLAN = "P1"


def _graph(tmp_path, *, changed=("nk_a",), refactor=(), added=()):
    """run 1 (P0) creates every node; run 2 (P1): `changed` get a struct change,
    `refactor` are matched + renamed with no struct change, `added` are new."""
    path = tmp_path / "proj-state-graph.db"
    c = ps.build(path)
    ps.add_run(c, 1, plan_id="P0")
    keys = list(changed) + list(refactor)
    for i, k in enumerate(keys, 1):
        ps.add_snapshot(c, 1, k, f"pkg.m.{k[3:]}_fn")
        ps.add_event(c, 1, i, "node_added", k)
    ps.add_run(c, 2, plan_id=PLAN, step_id=f"{PLAN}-REVIEW.1")
    seq = 0
    for k in changed:
        ps.add_snapshot(c, 2, k, f"pkg.m.{k[3:]}_fn", struct_sig="s2")
        seq += 1; ps.add_event(c, 2, seq, "node_matched", k, '{"via": "qualname"}')
        seq += 1; ps.add_event(c, 2, seq, "node_changed", k, '{"changed": ["struct_sig"]}')
    for k in refactor:
        ps.add_snapshot(c, 2, k, f"pkg.n.{k[3:]}_fn")
        seq += 1; ps.add_event(c, 2, seq, "node_matched", k, '{"via": "struct_sig"}')
        seq += 1; ps.add_event(c, 2, seq, "node_renamed", k, json.dumps({"from": f"pkg.m.{k[3:]}_fn", "to": f"pkg.n.{k[3:]}_fn"}))
    for k in added:
        ps.add_snapshot(c, 2, k, f"pkg.m.{k[3:]}_fn")
        seq += 1; ps.add_event(c, 2, seq, "node_added", k)
    c.commit(); c.close()
    return str(path)


def _plan(conn, steps=("A", "B")):
    """A COMPLETED-looking plan P1 of project proj with plain COMMAND steps (raw rows: no id rename, FKs stay happy)."""
    conn.execute("INSERT INTO Plans (plan_id, original_goal, status, project, project_source, created_at) VALUES "
                 "(?, 'goal', 'IN_PROGRESS', 'proj', 'declared', '2026-09-15 10:00:00')", (PLAN,))
    for i, s in enumerate(steps):
        conn.execute("INSERT INTO Steps (step_id, plan_id, description, status, execution_order, depth_level, log_context, step_type) "
                     "VALUES (?, ?, ?, 'PENDING', ?, 0, '', 'COMMAND')", (f"{PLAN}-{s}", PLAN, f"step {s}", i))
    conn.commit()
    return PLAN


def _log(conn, step_id, status, log):
    conn.execute("UPDATE Steps SET status=?, log_context=?, step_type='COMMAND', started_at=strftime('%Y-%m-%d %H:%M:%S','now') WHERE step_id=?",
                 (status, log, step_id))
    conn.commit()


@pytest.fixture
def graph(tmp_path):
    return _graph(tmp_path)


def _eval(conn, graph):
    return triggers.evaluate(conn, project="proj", plan_id=PLAN, psg_db_path=graph, commit=True)


def _reasons(conn):
    return [(r["node_key"], r["tier"], r["rule_id"], r["role"]) for r in pv.reasons_for_plan(conn, PLAN)]


def _log_rows(conn):
    return [tuple(r) for r in conn.execute("SELECT node_key, rule_id, verdict FROM trigger_log WHERE plan_id=? ORDER BY id", (PLAN,))]


# ── R1 ────────────────────────────────────────────────────────────────────────
def test_r1_test_fixed_hit(conn, graph):
    _plan(conn)
    _log(conn, f"{PLAN}-A", "FAILED", "pytest -q\n1 failed\nFAILED tests/test_m.py::test_a_fn")
    _log(conn, f"{PLAN}-B", "COMPLETED", "pytest -q tests/test_m.py\n3 passed (a_fn)")
    r = _eval(conn, graph)
    assert r["auto"] == 1 and r["by_rule"]["R1"] == 1 and r["ask"] == 0
    assert _reasons(conn) == [("nk_a", "derived", "R1", "reason")]
    assert "P1-A" in pv.reasons_for_plan(conn, PLAN)[0]["interpretation"]


def test_r1_test_fixed_miss_when_nothing_failed_first(conn, graph):
    _plan(conn)
    _log(conn, f"{PLAN}-B", "COMPLETED", "pytest -q\n3 passed (a_fn)")
    r = _eval(conn, graph)
    assert r["auto"] == 0 and r["ask"] == 1 and _log_rows(conn) == [("nk_a", None, "ask")]


# ── R2 ────────────────────────────────────────────────────────────────────────
def test_r2_gate_response_hit_and_miss(conn, graph):
    conn.execute("INSERT INTO Plans (plan_id, original_goal, status, project, project_source, created_at) VALUES "
                 "('P0', 'g', 'COMPLETED', 'proj', 'declared', '2026-09-15 09:00:00')")
    conn.execute("INSERT INTO Steps (step_id, plan_id, description, status, execution_order, depth_level, is_review, log_context) VALUES "
                 "('P0-REVIEW', 'P0', 'r', 'COMPLETED', 9, 0, 1, '[3] gates\\n  [fail] dtype_consistency_e2e: pkg/m.py a_fn returns int != dict')")
    conn.commit()
    _plan(conn)
    r = _eval(conn, graph)
    assert r["by_rule"]["R2"] == 1 and _reasons(conn)[0][2] == "R2"
    assert "responds to a failed gate in P0" in pv.reasons_for_plan(conn, PLAN)[0]["interpretation"]
    # a previous review that passed everything is not a response
    conn.execute("UPDATE Steps SET log_context='[3] gates\\n  [ok] dtype_consistency_e2e' WHERE step_id='P0-REVIEW'"); conn.commit()
    g2 = _graph(Path(graph).parent / "g2", changed=("nk_a",)) if False else None
    assert triggers.r2_gate_response(triggers._ctx(conn, "proj", PLAN, graph), {"node_key": "nk_a", "qualified_name": "pkg.m.a_fn", "file_path": "pkg/m.py"}) is None


# ── R3 ────────────────────────────────────────────────────────────────────────
def test_r3_upstream_drift_hit_and_miss(conn, graph):
    _plan(conn)
    import sqlite3
    g = sqlite3.connect(graph)
    g.executescript("""
        CREATE TABLE IF NOT EXISTS consistency_card (symbol_id INTEGER PRIMARY KEY, card_json TEXT NOT NULL);
        INSERT INTO node_type (id, name) VALUES (1, 'function');
        INSERT INTO node (id, node_type_id, name, qualified_name, file_path, run_id, node_key) VALUES (7, 1, 'a_fn', 'pkg.m.a_fn', 'pkg/m.py', 2, 'nk_a');
        INSERT INTO consistency_card VALUES (7, '{"callers": [], "reads": ["orders"], "lineage_upstream": []}');
    """)
    g.commit(); g.close()
    db.insert_llm_decision(conn, decision="halt", plan_id=PLAN, step_id=None, dataset="orders", column="amount",
                           observed_before="float", observed_after="str", drift_kind="dtype_changed", rationale="x",
                           action="halt", outcome="halted", failure=True)
    r = _eval(conn, graph)
    assert r["by_rule"]["R3"] == 1 and "orders.amount drifted (dtype_changed)" in pv.reasons_for_plan(conn, PLAN)[0]["interpretation"]
    ctx = triggers._ctx(conn, "proj", PLAN, graph)
    assert triggers.r3_upstream_drift(ctx, {"node_key": "nk_z", "qualified_name": "pkg.m.unrelated", "file_path": "pkg/m.py"}) is None


# ── R4 ────────────────────────────────────────────────────────────────────────
def test_r4_pure_refactor_hit_and_miss(conn, tmp_path):
    graph = _graph(tmp_path, changed=("nk_a",), refactor=("nk_r",))
    _plan(conn)
    r = _eval(conn, graph)
    assert r["by_rule"]["R4"] == 1 and ("nk_r", "derived", "R4", "reason") in _reasons(conn)
    assert r["ask"] == 1 and ("nk_a", None, "ask") in _log_rows(conn)          # the struct change is not a refactor
    basis = next(x["interpretation"] for x in pv.reasons_for_plan(conn, PLAN) if x["node_key"] == "nk_r")
    assert "struct_sig unchanged" in basis and "node_renamed" in basis


# ── R5 ────────────────────────────────────────────────────────────────────────
def test_r5_constraint_covered_hit_and_miss(conn, graph):
    _plan(conn)
    conn.execute("INSERT INTO LedgerEntries (project, kind, subjects, keywords, statement, rationale, status) VALUES "
                 "('proj', 'constraint', '[\"nk_a\"]', '[]', 'a_fn keeps paid orders only', 'finance', 'active')")
    conn.commit()
    r = _eval(conn, graph)
    assert r["by_rule"]["R5"] == 1 and "keeps paid orders" in pv.reasons_for_plan(conn, PLAN)[0]["interpretation"]
    conn.execute("UPDATE LedgerEntries SET status='superseded'"); conn.commit()
    ctx = triggers._ctx(conn, "proj", PLAN, graph)
    assert triggers.r5_constraint_covered(ctx, {"node_key": "nk_a", "qualified_name": "pkg.m.a_fn"}) is None


# ── R6 ────────────────────────────────────────────────────────────────────────
def test_r6_deviation_recovered_writes_a_derived_rejected_path(conn, graph):
    _plan(conn)
    api.start_step(conn, f"{PLAN}-A")
    api.fail_step(conn, f"{PLAN}-A", reason="a_fn returned a dict; the caller wanted a frame")
    _log(conn, f"{PLAN}-A", "FAILED", "x" * 500 + "\nTypeError: a_fn returned dict")
    api.evaluate_and_update_plan(conn, deviation_detected=True, target_step_id=f"{PLAN}-A",
                                 justification="retry a_fn with the frame return", new_sub_steps=["retry"])
    n = triggers.rejected_paths(conn, project="proj", plan_id=PLAN, psg_db_path=graph, commit=True)
    rows = pv.reasons_for_plan(conn, PLAN, role="rejected_path")
    assert n == 1 and all(r["tier"] == "derived" and r["rule_id"] == "R6" and r["node_key"] == "nk_a" for r in rows)
    failed = rows[0]
    assert failed["step_id"] == f"{PLAN}-A" and failed["interpretation"].startswith("step A")
    assert "a_fn returned a dict" in failed["interpretation"] and "deviation: retry a_fn" in failed["interpretation"]
    assert "TypeError" in failed["interpretation"] and len(failed["interpretation"].split("\n", 1)[1]) <= triggers.ERROR_TAIL
    assert triggers.rejected_paths(conn, project="proj", plan_id=PLAN, psg_db_path=graph, commit=True) == 0
    assert [x for x in _log_rows(conn) if x[1] == "R6"] and all(x[2] == "auto" for x in _log_rows(conn) if x[1] == "R6")


def test_r6_miss_without_failures_or_deviations(conn, graph):
    _plan(conn)
    assert triggers.rejected_paths(conn, project="proj", plan_id=PLAN, psg_db_path=graph, commit=True) == 0
    assert pv.reasons_for_plan(conn, PLAN, role="rejected_path") == []


# ── C1 / C2 / C4 and idempotence ─────────────────────────────────────────────
def test_c1_derivable_not_asked(conn, tmp_path):
    graph = _graph(tmp_path, changed=("nk_a",), refactor=("nk_r",))
    _plan(conn)
    _log(conn, f"{PLAN}-A", "FAILED", "pytest\n1 failed")
    _log(conn, f"{PLAN}-B", "COMPLETED", "pytest\n2 passed a_fn")
    _eval(conn, graph)
    from orchestrator import reasons
    assert reasons.slots_for_plan(conn, "proj", PLAN, graph) == []          # both nodes explained by a rule
    assert {x[2] for x in _log_rows(conn)} == {"auto"}


def test_c2_no_clue_asked(conn, tmp_path):
    graph = _graph(tmp_path, changed=("nk_a", "nk_b"))
    _plan(conn)
    r = _eval(conn, graph)
    from orchestrator import reasons
    assert r["ask"] == 2 and [s["node_key"] for s in reasons.slots_for_plan(conn, "proj", PLAN, graph)] == ["nk_a", "nk_b"]


def test_c4_every_verdict_logged(conn, tmp_path):
    graph = _graph(tmp_path, changed=("nk_a", "nk_b"), refactor=("nk_r",), added=("nk_n",))
    _plan(conn)
    r = _eval(conn, graph)
    rows = _log_rows(conn)
    assert len(rows) == r["auto"] + r["ask"] + r["silent"] == 4          # every touched, changed-or-derived node has a verdict
    assert {x[0] for x in rows} == {"nk_a", "nk_b", "nk_r", "nk_n"}


def test_evaluate_is_idempotent(conn, tmp_path):
    graph = _graph(tmp_path, changed=("nk_a",), refactor=("nk_r",))
    _plan(conn)
    first = _eval(conn, graph)
    second = _eval(conn, graph)
    assert first["auto"] == 1 and second == {"auto": 0, "ask": 0, "silent": 0, "by_rule": {k: 0 for k in second["by_rule"]}, "nodes": []}
    assert len(_reasons(conn)) == 1 and len(_log_rows(conn)) == 2


def test_pending_mode_logs_silent_not_ask(conn, graph):
    _plan(conn)
    r = triggers.evaluate(conn, project="proj", plan_id=PLAN, psg_db_path=graph, ask=False, commit=True)
    assert r["silent"] == 1 and r["ask"] == 0 and _log_rows(conn) == [("nk_a", None, "silent")]


def test_auto_filled_lists_rule_and_basis(conn, tmp_path):
    graph = _graph(tmp_path, refactor=("nk_r",))
    _plan(conn)
    _eval(conn, graph)
    af = triggers.auto_filled(conn, PLAN)
    assert af and af[0]["node_key"] == "nk_r" and af[0]["rule_id"] == "R4" and "pure refactor" in af[0]["basis"]
