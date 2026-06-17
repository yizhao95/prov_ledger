"""RED tests for analyzer.graph_viz — multi-level state-graph visualization.

Levels:
  - subsystems: 2-level-dir aggregation, weighted cross-subsystem edges
  - functions : app function/method/route nodes + calls/data-flow edges
  - full      : everything (parity with the old export_viz)
"""
import sqlite3

import pytest

from analyzer import (
    graph_viz,
    store,
    walker,
    py_ast,
    dataflow_types,
    pipeline,
    profiles,
)


@pytest.fixture
def db_path(tmp_path):
    repo = tmp_path / "repo"
    (repo / "app").mkdir(parents=True)
    (repo / "app" / "core.py").write_text(
        "import os\n\n\n"
        "def helper() -> list[int]:\n    return [1, 2]\n\n\n"
        "def sink(rows):\n    return len(rows)\n\n\n"
        "def main():\n    data = helper()\n    return sink(data)\n"
    )
    (repo / "tests").mkdir(parents=True)
    (repo / "tests" / "test_core.py").write_text(
        "def test_main():\n    assert True\n"
    )
    path = str(tmp_path / "demo-state-graph.db")
    conn = store.init_db(path)
    fm = walker.walk(conn, str(repo))
    py_ast.analyze(conn, str(repo), fm)
    dataflow_types.analyze(conn, str(repo), fm)
    pipeline.analyze(conn, str(repo), fm)
    profiles.analyze(conn, str(repo), fm)
    conn.close()
    return path


# ---- profiles / sub-flow lens + focus ----

def test_functions_nodes_carry_profile_tags(db_path):
    data = graph_viz.build_functions(db_path)
    # every app node exposes a (possibly empty) profiles list
    assert all("profiles" in n for n in data["nodes"])
    # top-level profiles catalogue is present
    assert "profiles" in data
    # main produces/consumes data => data-flow tag should appear somewhere
    all_tags = {p for n in data["nodes"] for p in n["profiles"]}
    assert "data-flow" in all_tags


def test_full_nodes_carry_profile_tags(db_path):
    data = graph_viz.build_full(db_path)
    assert "profiles" in data


# ---- data_var connectivity flags (false-positive 'isolated' fix) ----

def test_data_var_nodes_have_connectivity_flag(db_path):
    """Every data_var carries an `unconsumed` bool so the lens can tell a
    genuinely-terminal/dangling value from one that flows onward."""
    data = graph_viz.build_functions(db_path, include_data_vars=True)
    dvs = [n for n in data["nodes"] if n["node_type"] == "data_var"]
    assert dvs, "expected data_var nodes in the function-level graph"
    assert all("unconsumed" in n for n in dvs)


def test_consumed_return_is_not_flagged_unconsumed(db_path):
    """helper()'s return IS consumed by main (data = helper()), so it must NOT
    be flagged unconsumed."""
    data = graph_viz.build_functions(db_path, include_data_vars=True)
    helper_ret = [
        n for n in data["nodes"]
        if n["node_type"] == "data_var" and "helper" in n["label"]
        and "return" in n["label"]
    ]
    assert helper_ret, "expected a helper:return data_var"
    assert all(n["unconsumed"] is False for n in helper_ret)


def test_html_has_subflow_lens_and_focus(tmp_path, db_path):
    out = tmp_path / "viz-fn.html"
    graph_viz.write_html(db_path, str(out), level="functions", title="demo-app")
    html = out.read_text()
    # sub-flow lens control + click-to-focus behavior present
    assert "subflow" in html.lower()
    assert "focus" in html.lower()


# ---- subsystems (high level) ----

def test_subsystems_aggregates_and_has_legend(db_path):
    data = graph_viz.build_subsystems(db_path)
    assert "nodes" in data and "edges" in data and "legend" in data
    # node ids are subsystem strings, not raw node ids
    ids = {n["id"] for n in data["nodes"]}
    assert any("/" in i or i in {"app", "tests", "(root)"} for i in ids)


def test_subsystems_excludes_external_bucket(db_path):
    data = graph_viz.build_subsystems(db_path)
    ids = {n["id"] for n in data["nodes"]}
    assert "(external)" not in ids
    assert "(unknown)" not in ids


def test_subsystems_edges_are_cross_subsystem_only(db_path):
    data = graph_viz.build_subsystems(db_path)
    for e in data["edges"]:
        assert e["from"] != e["to"]


# ---- functions (mid level) ----

def test_functions_excludes_tests_and_nonfunc(db_path):
    data = graph_viz.build_functions(db_path)
    labels = {n["label"] for n in data["nodes"]}
    types = {n["node_type"] for n in data["nodes"]}
    assert "helper" in labels and "main" in labels
    assert "test_main" not in labels          # tests excluded
    assert types <= {"function", "method", "route"}  # no file/module/data_var


def test_functions_edges_only_between_surviving_nodes(db_path):
    data = graph_viz.build_functions(db_path)
    ids = {n["id"] for n in data["nodes"]}
    for e in data["edges"]:
        assert e["from"] in ids and e["to"] in ids


def test_functions_data_vars_gated(db_path):
    without = graph_viz.build_functions(db_path, include_data_vars=False)
    with_dv = graph_viz.build_functions(db_path, include_data_vars=True)
    t_without = {n["node_type"] for n in without["nodes"]}
    t_with = {n["node_type"] for n in with_dv["nodes"]}
    assert "data_var" not in t_without
    assert "data_var" in t_with


# ---- full ----

def test_full_has_type_lists(db_path):
    data = graph_viz.build_full(db_path)
    assert set(data.keys()) >= {"nodes", "edges", "node_types", "edge_types"}
    assert "function" in data["node_types"]
    assert "file" in data["node_types"]


# ---- write_html ----

@pytest.mark.parametrize("level", ["subsystems", "functions", "full"])
def test_write_html_self_contained(tmp_path, db_path, level):
    out = tmp_path / f"viz-{level}.html"
    graph_viz.write_html(db_path, str(out), level=level, title="demo-app")
    html = out.read_text()
    assert out.exists()
    assert "vis-network" in html       # CDN viewer embedded
    assert "demo-app" in html          # title injected
    assert len(html) > 1000            # non-trivial, data embedded
