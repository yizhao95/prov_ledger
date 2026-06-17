"""RED tests for analyzer.export_viz — JSON export for the interactive graph viewer."""
import pytest

from analyzer import export_viz, store, walker, py_ast


@pytest.fixture
def db_path(tmp_path):
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "mod.py").write_text(
        "import os\n\n\ndef helper():\n    return 1\n\n\ndef main():\n    return helper()\n"
    )
    path = str(tmp_path / "demo-state-graph.db")
    conn = store.init_db(path)
    fm = walker.walk(conn, str(repo))
    py_ast.analyze(conn, str(repo), fm)
    conn.close()
    return path


def test_export_returns_nodes_edges_and_type_lists(db_path):
    data = export_viz.export_graph(db_path)
    assert set(data.keys()) >= {"nodes", "edges", "node_types", "edge_types"}


def test_nodes_have_id_label_and_type(db_path):
    data = export_viz.export_graph(db_path)
    n = data["nodes"][0]
    assert "id" in n and "label" in n and "node_type" in n


def test_edges_have_from_to_and_type(db_path):
    data = export_viz.export_graph(db_path)
    e = data["edges"][0]
    assert "from" in e and "to" in e and "edge_type" in e


def test_type_lists_are_distinct_sorted(db_path):
    data = export_viz.export_graph(db_path)
    assert "function" in data["node_types"]
    assert "file" in data["node_types"]
    assert data["node_types"] == sorted(set(data["node_types"]))
    assert "defines" in data["edge_types"]


def test_every_edge_endpoint_exists_in_nodes(db_path):
    data = export_viz.export_graph(db_path)
    ids = {n["id"] for n in data["nodes"]}
    for e in data["edges"]:
        assert e["from"] in ids
        assert e["to"] in ids


def test_write_html_creates_self_contained_file(tmp_path, db_path):
    out = tmp_path / "viz.html"
    export_viz.write_html(db_path, str(out), title="demo-app")
    html = out.read_text()
    assert out.exists()
    # data is embedded so the file works via file:// with no server
    assert "GRAPH_DATA" in html
    assert "demo-app" in html
    # filter UI present
    assert "node_types" in html or "nodeType" in html
