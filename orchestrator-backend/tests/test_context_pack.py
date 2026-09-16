"""context_pack.build — one bounded read of a node's history and blast radius (DP phase 2, Task 2; H2, I7)."""
import json
import sqlite3
import sys
from pathlib import Path

import pytest

from orchestrator import context_pack as cp, constraints, provenance as pv, psg_bridge

sys.path.insert(0, str(Path(__file__).parent))
import _psg_schema as ps  # noqa: E402


@pytest.fixture
def graph(tmp_path):
    """nk_a was pkg.m.load (run 1) and is pkg.m.load_orders (run 2, renamed); a card with consumers; nk_x downstream."""
    path = tmp_path / "proj-state-graph.db"
    c = ps.build(path)
    ps.add_run(c, 1, plan_id="P0"); ps.add_snapshot(c, 1, "nk_a", "pkg.m.load"); ps.add_event(c, 1, 1, "node_added", "nk_a")
    ps.add_snapshot(c, 1, "nk_x", "pkg.m.clean"); ps.add_event(c, 1, 2, "node_added", "nk_x")
    ps.add_run(c, 2, plan_id="P1"); ps.add_snapshot(c, 2, "nk_a", "pkg.m.load_orders", struct_sig="s2"); ps.add_snapshot(c, 2, "nk_x", "pkg.m.clean")
    ps.add_event(c, 2, 1, "node_matched", "nk_a", '{"via": "struct_sig"}')
    ps.add_event(c, 2, 2, "node_renamed", "nk_a", json.dumps({"from": "pkg.m.load", "to": "pkg.m.load_orders"}))
    c.executescript("""
        CREATE TABLE IF NOT EXISTS consistency_card (symbol_id INTEGER PRIMARY KEY, card_json TEXT NOT NULL);
        INSERT INTO node_type (id, name) VALUES (1, 'function');
        INSERT INTO node (id, node_type_id, name, qualified_name, file_path, run_id, node_key) VALUES (7, 1, 'load_orders', 'pkg.m.load_orders', 'pkg/m.py', 2, 'nk_a');
        INSERT INTO node (id, node_type_id, name, qualified_name, file_path, run_id, node_key) VALUES (8, 1, 'clean', 'pkg.m.clean', 'pkg/m.py', 2, 'nk_x');
        INSERT INTO consistency_card VALUES (7, '{"callers": ["pkg.m.main"], "output_consumers": ["pkg.m.clean"], "dtype_map": {"return": "DataFrame"}, "lineage_downstream": ["orders_clean"], "reads": ["orders"]}');
        INSERT INTO consistency_card VALUES (8, '{"callers": ["pkg.m.load_orders"], "output_consumers": [], "dtype_map": {}, "lineage_downstream": []}');
    """)
    c.commit(); c.close()
    return str(path)


def _seed(conn, n_reasons=5, n_rejected=7):
    u = pv.insert_utterance(conn, session_id="s", project="proj", plan_id="P0", text="keep load_orders on paid orders only", occurred_at="2026-09-15 10:00:00")
    ids = {"constraints": [], "reasons": [], "rejected": [], "old_name": None, "neighbor": []}
    ids["constraints"].append(constraints.record_constraint(conn, project="proj", subjects=["nk_a"], statement="load_orders keeps paid orders only", rationale="finance", why_ref="docs/f.md"))
    ids["constraints"].append(constraints.record_constraint(conn, project="proj", subjects=["nk_a"], statement="never read raw prod", rationale="SECRET", why_visibility="restricted"))
    ids["old_name"] = pv.insert_reason(conn, project="proj", plan_id="P0", node_key="pkg.m.load", kind="technical", interpretation="recorded under the OLD name", recorded_by="agent")
    for i in range(n_reasons):
        ids["reasons"].append(pv.insert_reason(conn, project="proj", plan_id=f"P{i}", node_key="nk_a", kind="technical",
                                               interpretation=f"reason {i} " + "x" * 60, recorded_by="agent"))
    minor = pv.insert_reason(conn, project="proj", plan_id="P9", node_key="nk_a", kind="technical", interpretation="a minor one", recorded_by="agent")
    conn.execute("INSERT INTO significance_log (reason_id, project, hint, hint_basis, judged_by) VALUES (?, 'proj', 'minor', 'nothing hit', 'hint')", (minor,))   # DP 2b: significance_eff, not the stored column
    ids["reasons"].append(minor)
    for i in range(n_rejected):
        ids["rejected"].append(pv.insert_reason(conn, project="proj", plan_id=f"P{i}", node_key="nk_a", kind="technical", role="rejected_path",
                                                interpretation=f"tried {i} " + "y" * 60, rule_id="R6", recorded_by="system"))
    for i in range(3):
        ids["neighbor"].append(constraints.record_constraint(conn, project="proj", subjects=["nk_x"], statement=f"clean rule {i}", rationale="r"))
    pv.insert_reason(conn, project="proj", plan_id="P0", node_key="nk_x", kind="technical", role="rejected_path", interpretation="clean tried", rule_id="R6", recorded_by="system")
    conn.commit()
    return ids


def test_layers_caps_identity_chain_and_neighbor_counts(conn, graph):
    ids = _seed(conn)
    pack = cp.build(conn, project="proj", targets=["pkg.m.load_orders"], psg_db_path=graph, budget_tokens=100000, record=False)
    t = pack.targets[0]
    assert t.node_key == "nk_a" and t.status == "existing" and t.identity_chain[0] == "pkg.m.load_orders" and "pkg.m.load" in t.identity_chain
    assert [c["id"] for c in t.constraints] == sorted(ids["constraints"], reverse=True) and all(c["role"] == "constraint" for c in t.constraints)
    assert t.counts == {"constraints": 2, "rejected_paths": 7, "reasons": 7, "reasons_minor": 1, "callers": 1}
    assert len(t.rejected_paths) == 5 and len(t.reasons) == 3                  # caps: rejected 5, reasons 3
    assert ids["old_name"] in [r["id"] for r in t.reasons] or ids["old_name"] in [r["id"] for r in cp._records(conn, "proj", ["pkg.m.load"])]
    assert all(r.get("significance") != "minor" for r in t.reasons)
    assert pack.truncated == {"rejected_paths": 2, "reasons": 3, "reasons_minor": 1}
    assert any("还有 2 条 rejected paths" in h and "provledger why pkg.m.load_orders --all" in h for h in pack.hints)
    assert t.callers == ["pkg.m.main"] and t.output_consumers == ["pkg.m.clean"] and t.dtype_map == {"return": "DataFrame"}
    assert t.upstream_assumptions == [{"table": "orders"}]
    assert pack.neighbors_counts == {"pkg.m.clean": {"constraints": 3, "rejected_paths": 1}}     # counts only by default
    assert "SECRET" not in json.dumps(pack.as_dict())                                            # a personal rationale never travels
    assert pack.approx_tokens > 0 and pack.shown == 2 + 5 + 3


def test_neighbors_constraints_mode_caps_at_two_per_neighbor(conn, graph):
    _seed(conn)
    pack = cp.build(conn, project="proj", targets=["pkg.m.load_orders"], psg_db_path=graph, neighbors="constraints", budget_tokens=100000, record=False)
    nb = pack.neighbors_counts["pkg.m.clean"]
    assert nb["constraints"] == 3 and len(nb["statements"]) == 2 and pack.truncated["neighbor_constraints"] == 1


def test_i7_budget_trims_reasons_then_rejected_then_neighbors_then_constraints(conn, graph):
    _seed(conn)
    big = cp.build(conn, project="proj", targets=["pkg.m.load_orders"], psg_db_path=graph, neighbors="constraints", budget_tokens=100000, record=False)
    full = big.approx_tokens
    pack = cp.build(conn, project="proj", targets=["pkg.m.load_orders"], psg_db_path=graph, neighbors="constraints", budget_tokens=full - 150, record=False)
    t = pack.targets[0]
    assert len(t.reasons) < 3 and len(t.rejected_paths) == 5 and len(t.constraints) == 2      # reasons go first
    tiny = cp.build(conn, project="proj", targets=["pkg.m.load_orders"], psg_db_path=graph, neighbors="constraints", budget_tokens=200, record=False)
    t = tiny.targets[0]
    # DP phase 2 (Task 4): the trim never cuts a target's kind to zero — the structure alone can exceed
    # the budget (the Task 4 plan's pack lost all 12 reasons that way); the last record of each kind stays
    assert len(t.reasons) == 1 and len(t.rejected_paths) == 1 and tiny.neighbors_counts["pkg.m.clean"].get("statements", []) == []
    assert tiny.truncated["reasons"] >= 2 and tiny.truncated["rejected_paths"] >= 4
    assert tiny.approx_tokens > 200 and len(t.constraints) == 1                                # constraints are the last to go, and one stays
    assert tiny.hints and all("provledger why" in h for h in tiny.hints)


def test_record_writes_one_read_hit_per_shown_record_once_per_plan_and_moment(conn, graph):
    ids = _seed(conn)
    pack = cp.build(conn, project="proj", targets=["pkg.m.load_orders"], psg_db_path=graph, budget_tokens=100000, plan_id="P7", moment="plan")
    rows = conn.execute("SELECT reason_id, plan_id, moment FROM read_hit ORDER BY id").fetchall()
    assert len(rows) == pack.shown and {r[1] for r in rows} == {"P7"} and {r[2] for r in rows} == {"plan"}
    assert set(ids["constraints"]) <= {r[0] for r in rows} and set(ids["rejected"]) - {r[0] for r in rows}
    cp.build(conn, project="proj", targets=["pkg.m.load_orders"], psg_db_path=graph, budget_tokens=100000, plan_id="P7", moment="plan")
    assert conn.execute("SELECT COUNT(*) FROM read_hit").fetchone()[0] == len(rows)          # recomputed = not a new showing
    cp.build(conn, project="proj", targets=["pkg.m.load_orders"], psg_db_path=graph, budget_tokens=100000, plan_id="P7", moment="why")
    assert conn.execute("SELECT COUNT(*) FROM read_hit WHERE moment='why'").fetchone()[0] == len(rows)
    assert conn.execute("SELECT COUNT(*) FROM influence").fetchone()[0] == 0                   # I11: shown is not adopted


def test_record_false_writes_nothing(conn, graph):
    _seed(conn)
    cp.build(conn, project="proj", targets=["pkg.m.load_orders"], psg_db_path=graph, record=False, plan_id="P7")
    assert conn.execute("SELECT COUNT(*) FROM read_hit").fetchone()[0] == 0


def test_h2_build_opens_the_graph_once(conn, graph, monkeypatch):
    _seed(conn)
    calls = []
    real = psg_bridge.open_ro

    def counting(path):
        calls.append(path)
        return real(path)
    monkeypatch.setattr(psg_bridge, "open_ro", counting)
    cp.build(conn, project="proj", targets=["pkg.m.load_orders", "pkg.m.clean", "pkg.m.nope"], psg_db_path=graph, neighbors="constraints", record=False)
    assert calls == [graph]


def test_unknown_target_and_missing_graph_degrade_visibly(conn, graph):
    pack = cp.build(conn, project="proj", targets=["pkg.m.nope"], psg_db_path=graph, record=False)
    assert pack.targets[0].status == "new" and pack.targets[0].node_key is None and pack.shown == 0
    pack2 = cp.build(conn, project="proj", targets=["pkg.m.load_orders"], psg_db_path=str(Path(graph).parent / "missing.db"), record=False)
    assert pack2.targets[0].status == "new" and pack2.targets[0].identity_chain == ["pkg.m.load_orders"]
    assert pack2.approx_tokens > 0 and "generated_at" in pack2.as_dict()


def test_structure_folds_into_counts_when_records_alone_cannot_reach_the_budget(conn, graph):
    """DP phase 2 close-out (H4): a target with hundreds of callers blew a real pack to
    4351 tokens while the trim only ever cut records. Over budget after the record
    floor, callers are capped, lineage / dtype_map become counts, and every fold is
    counted and hinted."""
    _seed(conn)
    g = sqlite3.connect(graph)
    callers = [f"pkg.tests.test_case_{i}.test_load_orders_{i}" for i in range(120)]
    g.execute("UPDATE consistency_card SET card_json = ? WHERE symbol_id = 7",
              (json.dumps({"callers": callers, "callees": [], "output_consumers": ["pkg.m.clean"], "dtype_map": {f"col{i}": "int64" for i in range(30)},
                           "lineage_downstream": [f"pkg.m.report_{i}" for i in range(20)], "reads": []}),))
    g.commit(); g.close()
    pack = cp.build(conn, project="proj", targets=["pkg.m.load_orders"], psg_db_path=graph, budget_tokens=300, record=False)
    t = pack.targets[0]
    assert len(t.callers) == cp.FOLD_CALLERS and t.counts["callers"] == 120 and pack.truncated["callers"] == 120 - len(t.callers)
    assert t.dtype_map == {} and t.counts["dtype_map"] == 30 and t.lineage_downstream == [] and t.counts["lineage_downstream"] == 20
    assert any("结构" in h or "callers" in h for h in pack.hints)
    assert pack.approx_tokens < 1800                                   # not necessarily under 900: the floor keeps one record per kind
    big = cp.build(conn, project="proj", targets=["pkg.m.load_orders"], psg_db_path=graph, budget_tokens=100000, record=False)
    assert len(big.targets[0].callers) == cp.CAP_CALLERS and big.targets[0].counts["callers"] == 120        # callers are always capped
    assert big.targets[0].dtype_map and big.targets[0].lineage_downstream                                  # nothing else folds under a large budget
