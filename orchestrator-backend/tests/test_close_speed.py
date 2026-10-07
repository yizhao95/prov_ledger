"""The plan close reads the state graph without repeating itself (FL-211).

On prov_ledger's own graph (338k snapshots) triggers.evaluate took 50-583 s per
close: every lookup of a node's latest snapshot by node_key scanned the whole
table — the only index led with run_id — and R0's file-name rule recomputed the
plan's changed nodes on every call. The review's write-script ceiling is 60 s,
so every close was cut, rolled back, and closed by hand without its rules ever
writing a reason; and while it ran it held the ledger's write lock, which made
the hooks drop rows (FL-193).
"""
import sqlite3
import sys
from pathlib import Path

from orchestrator import provenance as pv, psg_bridge, triggers

sys.path.insert(0, str(Path(__file__).parent))
import _psg_schema as ps  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
PLAN = "speed-1"
NODES = {"nk_a": ("pkg.m.load", "pkg/m.py"), "nk_b": ("pkg.m.run", "pkg/m.py"),
         "nk_c": ("pkg.clean.rows", "pkg/clean.py"), "nk_d": ("pkg.io.save", "pkg/io.py")}


def _graph(tmp_path):
    path = tmp_path / "g.db"
    c = ps.build(path)
    ps.add_run(c, 1, plan_id="P0")
    ps.add_run(c, 2, plan_id=PLAN, step_id=f"{PLAN}-REVIEW.1")
    for i, (k, (qn, fp)) in enumerate(NODES.items(), 1):
        for run, sig in ((1, "s1"), (2, "s2")):
            c.execute("INSERT INTO node_snapshot (run_id, node_key, node_type, qualified_name, file_path, struct_sig, "
                      "dataflow_sig, dataflow_trivial, attrs_json) VALUES (?, ?, 'function', ?, ?, ?, NULL, 1, '{}')",
                      (run, k, qn, fp, sig))
        ps.add_event(c, 1, i, "node_added", k)
        ps.add_event(c, 2, i, "node_changed", k, '{"changed": ["struct_sig"]}')
    c.commit(); c.close()
    return str(path)


def test_a_nodes_latest_snapshot_is_found_through_an_index(tmp_path):
    c = sqlite3.connect(_graph(tmp_path))
    plan = " ".join(r[3] for r in c.execute(
        "EXPLAIN QUERY PLAN SELECT file_path FROM node_snapshot WHERE node_key = ? ORDER BY run_id DESC, id DESC LIMIT 1",
        ("nk_a",)))
    assert "USING" in plan and "INDEX" in plan and not plan.startswith("SCAN"), plan
    events = " ".join(r[3] for r in c.execute(
        "EXPLAIN QUERY PLAN SELECT e.node_key FROM node_event e JOIN analysis_run a ON a.id = e.run_id WHERE a.plan_id = ?",
        (PLAN,)))
    assert "SCAN e" not in events and "SCAN node_event" not in events, events
    c.close()


def test_the_analyzer_and_the_test_schema_carry_the_same_indexes():
    store = (REPO / "skills/project-state-graph/scripts/analyzer/store.py").read_text(encoding="utf-8")
    copy = (REPO / "orchestrator-backend/tests/_psg_schema.py").read_text(encoding="utf-8")
    for stmt in ("ON node_snapshot(node_key, run_id)", "ON node_event(run_id)"):
        assert stmt in store and stmt in copy, stmt


def test_the_close_computes_the_plans_changed_nodes_once(conn, tmp_path, monkeypatch):
    graph = _graph(tmp_path)
    conn.execute("INSERT INTO Plans (plan_id, original_goal, status, project, project_source, created_at) VALUES "
                 "(?, 'goal', 'IN_PROGRESS', 'proj', 'declared', '2026-09-15 10:00:00')", (PLAN,))
    conn.commit()
    for text in ("m.py has to stop reading the discount column", "and clean.py should drop empty rows",
                 "keep io.py as it is", "run should not retry"):
        pv.insert_utterance(conn, session_id="s", project="proj", plan_id=PLAN, text=text, occurred_at="2026-09-15 10:05:00")
    calls = []
    real = psg_bridge._changed_node_keys           # the query itself, behind the cache

    def counting(path, plan_id):
        calls.append(plan_id)
        return real(path, plan_id)

    monkeypatch.setattr(psg_bridge, "_changed_node_keys", counting)
    triggers.evaluate(conn, project="proj", plan_id=PLAN, psg_db_path=graph, commit=True)
    assert len(calls) == 1, f"changed_node_keys ran {len(calls)} times in one close"


def _regex_names_in(sentence, names):
    """The reference: the literal-name rule as it was written, one pattern per name."""
    return {n for n in names if triggers._literal(n).search(sentence)}


def test_the_fast_name_match_finds_exactly_what_the_regex_rule_finds():
    """R0's local-name rule matched every touched node's names against every
    sentence with one regex each — 16.9M searches in one close. The dictionary
    lookup must find exactly the same names, boundaries included."""
    names = ["load", "load_orders", "pkg.m.load", "m.load", "run", "Run", "x1", "a.b", "clean_rows",
             "orders", "_private", "v2", "q3_emea_uplift", "rollup.weekly_totals", "total"]
    sentences = ["load_orders should stop", "see pkg.m.load.", "m.load and load", "rerun it", "Run it, run it",
                 "x1x1 x1", "a.b.c and a.bc", "clean_rows_v2 vs clean_rows", "the orders: orders", "__private _private",
                 "v2.0 and v2", "Q3 q3_emea_uplift", "rollup.weekly_totals_x rollup.weekly_totals", "total.", "",
                 "loadload load-orders load.orders"]
    index = triggers._name_index({n: [n] for n in names})
    for s in sentences:
        assert triggers._names_in(s, index) == _regex_names_in(s, names), s


def test_matching_names_compiles_no_pattern_per_name(monkeypatch):
    compiled = []
    real = triggers._literal
    monkeypatch.setattr(triggers, "_literal", lambda n: compiled.append(n) or real(n))
    index = triggers._name_index({"k1": ["load_orders", "pkg.m.load_orders"], "k2": ["clean"]})
    assert triggers._names_in("pkg.m.load_orders and clean", index) == {"k1", "k2"}
    assert compiled == []
