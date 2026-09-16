"""psg_bridge.runs_of / graph_at / latest_tier_of — the Graph view's data (DP phase 2b, Task 3)."""
import sys
from pathlib import Path

from orchestrator import psg_bridge

sys.path.insert(0, str(Path(__file__).parent))
import _psg_schema as ps  # noqa: E402


def _graph(tmp_path):
    """run 1: a, b (functions) + t (sql_table); run 2: a renamed, b gone, c added; latest node/edge tables = run 2 with a→c calls, a→t reads_sql."""
    path = tmp_path / "g.db"
    c = ps.build(path)
    ps.add_run(c, 1, plan_id="P0", sha="aaa"); ps.add_run(c, 2, plan_id="P1", sha="bbb", trigger="review")
    ps.add_snapshot(c, 1, "nk_a", "pkg.m.load"); ps.add_snapshot(c, 1, "nk_b", "pkg.m.old"); ps.add_snapshot(c, 1, "nk_t", "orders", ntype="sql_table")
    ps.add_event(c, 1, 1, "node_added", "nk_a"); ps.add_event(c, 1, 2, "node_added", "nk_b"); ps.add_event(c, 1, 3, "node_added", "nk_t")
    ps.add_snapshot(c, 2, "nk_a", "pkg.m.load_orders", struct_sig="s2"); ps.add_snapshot(c, 2, "nk_c", "pkg.m.clean"); ps.add_snapshot(c, 2, "nk_t", "orders", ntype="sql_table")
    ps.add_event(c, 2, 1, "node_renamed", "nk_a", '{"from": "pkg.m.load", "to": "pkg.m.load_orders"}', tier="derived")
    ps.add_event(c, 2, 2, "node_removed", "nk_b"); ps.add_event(c, 2, 3, "node_added", "nk_c")
    c.executescript("""
        INSERT INTO node_type (id, name) VALUES (1, 'function'), (2, 'sql_table');
        INSERT INTO node (id, node_type_id, name, qualified_name, file_path, run_id, node_key) VALUES (1, 1, 'load_orders', 'pkg.m.load_orders', 'pkg/m.py', 2, 'nk_a'), (2, 1, 'clean', 'pkg.m.clean', 'pkg/m.py', 2, 'nk_c'), (3, 2, 'orders', 'orders', NULL, 2, 'nk_t');
        CREATE TABLE IF NOT EXISTS edge_type (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE);
        CREATE TABLE IF NOT EXISTS edge (id INTEGER PRIMARY KEY, edge_type_id INTEGER NOT NULL, src_node_id INTEGER NOT NULL, dst_node_id INTEGER NOT NULL, metadata_json TEXT, run_id INTEGER, confidence TEXT);
        INSERT INTO edge_type (id, name) VALUES (1, 'calls'), (2, 'reads_sql');
        INSERT INTO edge (edge_type_id, src_node_id, dst_node_id) VALUES (1, 1, 2), (2, 1, 3);
    """)
    c.commit(); c.close()
    return str(path)


def test_runs_of_newest_first(tmp_path):
    g = _graph(tmp_path)
    runs = psg_bridge.runs_of(g)
    assert [r["run_id"] for r in runs] == [2, 1] and runs[0]["commit_sha"] == "bbb" and runs[0]["plan_id"] == "P1" and runs[0]["trigger"] == "review"
    assert psg_bridge.runs_of(None) == []


def test_graph_at_latest_and_at_a_run_differ_and_edges_say_where_they_come_from(tmp_path):
    g = _graph(tmp_path)
    now = psg_bridge.graph_at(g)
    assert now["run_id"] == 2 and now["edges_from"] == "run" and now["level"] == "functions"
    assert sorted(n["node_key"] for n in now["nodes"]) == ["nk_a", "nk_c"]                       # functions only: the sql_table is out
    assert now["edges"] == [{"src_key": "nk_a", "dst_key": "nk_c", "edge_type": "calls"}]
    then = psg_bridge.graph_at(g, run_id=1)
    assert then["run_id"] == 1 and then["edges_from"] == "latest"                                # edges have no run dimension — said, not hidden
    assert sorted(n["node_key"] for n in then["nodes"]) == ["nk_a", "nk_b"]
    assert next(n for n in then["nodes"] if n["node_key"] == "nk_a")["qualified_name"] == "pkg.m.load"   # the name it carried then
    assert then["edges"] == []                                                                     # a→c: c did not exist at run 1


def test_graph_at_full_level_keeps_every_type_and_edge(tmp_path):
    g = _graph(tmp_path)
    full = psg_bridge.graph_at(g, level="full")
    assert sorted(n["node_key"] for n in full["nodes"]) == ["nk_a", "nk_c", "nk_t"]
    assert sorted(e["edge_type"] for e in full["edges"]) == ["calls", "reads_sql"]
    assert psg_bridge.graph_at(None) == {"nodes": [], "edges": [], "run_id": None, "edges_from": "none", "level": "functions"}


def test_latest_tier_of_follows_the_last_event_up_to_the_run(tmp_path):
    g = _graph(tmp_path)
    assert psg_bridge.latest_tier_of(g)["nk_a"] == "derived" and psg_bridge.latest_tier_of(g, run_id=1)["nk_a"] == "observed"
    assert psg_bridge.latest_tier_of(g, run_id=1).get("nk_c") is None
