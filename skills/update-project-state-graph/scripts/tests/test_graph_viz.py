"""RED tests for graph_viz in update-project-state-graph (no analyzer package).

This skill has no analyzer package, so the tmp DB is built with raw sqlite,
mirroring the state-graph schema (node_type/node/edge_type/edge).
"""
import sqlite3

import pytest

import graph_viz


def _seed(path):
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE node_type(id INTEGER PRIMARY KEY, name TEXT UNIQUE);
        CREATE TABLE node(id INTEGER PRIMARY KEY, node_type_id INTEGER,
            name TEXT, qualified_name TEXT, file_path TEXT,
            line_start INTEGER, line_end INTEGER, metadata_json TEXT);
        CREATE TABLE edge_type(id INTEGER PRIMARY KEY, name TEXT UNIQUE);
        CREATE TABLE edge(id INTEGER PRIMARY KEY, edge_type_id INTEGER,
            src_node_id INTEGER, dst_node_id INTEGER, metadata_json TEXT);
        """
    )
    nt = {}
    for n in ("function", "method", "route", "data_var", "file"):
        cur = conn.execute("INSERT INTO node_type(name) VALUES(?)", (n,))
        nt[n] = cur.lastrowid
    et = {}
    for e in ("calls", "downstream_data_feed", "produces", "consumes", "defines"):
        cur = conn.execute("INSERT INTO edge_type(name) VALUES(?)", (e,))
        et[e] = cur.lastrowid

    def node(ntype, name, fp):
        cur = conn.execute(
            "INSERT INTO node(node_type_id,name,qualified_name,file_path,line_start)"
            " VALUES(?,?,?,?,1)", (nt[ntype], name, name, fp))
        return cur.lastrowid

    # subsystem app/svc
    loader = node("function", "loader", "app/svc/io.py")
    parser = node("function", "parser", "app/svc/io.py")
    route = node("route", "handle", "app/api/routes.py")
    dv = node("data_var", "loader:return", "app/svc/io.py")
    # subsystem tests (must be excluded from functions level)
    tfn = node("function", "test_loader", "tests/test_io.py")

    def edge(etype, s, d):
        conn.execute("INSERT INTO edge(edge_type_id,src_node_id,dst_node_id)"
                     " VALUES(?,?,?)", (et[etype], s, d))

    edge("calls", route, loader)              # cross subsystem app/api -> app/svc
    edge("downstream_data_feed", loader, parser)
    edge("produces", loader, dv)
    edge("consumes", dv, parser)
    edge("calls", tfn, loader)                # test edge, dropped at functions level
    conn.commit()
    conn.close()


@pytest.fixture
def db_path(tmp_path):
    p = str(tmp_path / "demo-state-graph.db")
    _seed(p)
    return p


def test_subsystems_aggregates_and_legend(db_path):
    data = graph_viz.build_subsystems(db_path)
    ids = {n["id"] for n in data["nodes"]}
    assert "app/svc" in ids and "app/api" in ids
    assert "legend" in data


def test_subsystems_cross_only(db_path):
    data = graph_viz.build_subsystems(db_path)
    for e in data["edges"]:
        assert e["from"] != e["to"]


def test_functions_excludes_tests(db_path):
    data = graph_viz.build_functions(db_path)
    labels = {n["label"] for n in data["nodes"]}
    assert "loader" in labels and "handle" in labels
    assert "test_loader" not in labels
    assert {n["node_type"] for n in data["nodes"]} <= {"function", "method", "route"}


def test_functions_data_vars_gated(db_path):
    t0 = {n["node_type"] for n in graph_viz.build_functions(db_path)["nodes"]}
    t1 = {n["node_type"] for n in graph_viz.build_functions(db_path, include_data_vars=True)["nodes"]}
    assert "data_var" not in t0 and "data_var" in t1


def test_functions_edges_between_surviving(db_path):
    data = graph_viz.build_functions(db_path)
    ids = {n["id"] for n in data["nodes"]}
    for e in data["edges"]:
        assert e["from"] in ids and e["to"] in ids
    assert "downstream_data_feed" in data["edge_types"]


def test_full_has_type_lists(db_path):
    data = graph_viz.build_full(db_path)
    assert set(data.keys()) >= {"nodes", "edges", "node_types", "edge_types"}
    assert "function" in data["node_types"]


@pytest.mark.parametrize("level", ["subsystems", "functions", "full"])
def test_write_html_self_contained(tmp_path, db_path, level):
    out = tmp_path / f"v-{level}.html"
    graph_viz.write_html(db_path, str(out), level=level, title="demo-up")
    html = out.read_text()
    assert "vis-network" in html and "demo-up" in html and len(html) > 1000
