"""RED tests for analyzer.dataflow — downstream_data_feed edges (output->input)."""
import pytest

from analyzer import dataflow, py_ast, store, walker

SAMPLE = '''\
def producer():
    return 1


def multi():
    return 1, 2


def consumer(v):
    return v + 1


def other(v):
    return v


def pipeline():
    x = producer()
    consumer(x)
    a, b = multi()
    other(a)


def unrelated():
    z = 5
    consumer(z)
'''


@pytest.fixture
def analyzed(tmp_path):
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "flow.py").write_text(SAMPLE)
    conn = store.init_db(str(tmp_path / "demo-state-graph.db"))
    file_map = walker.walk(conn, str(repo))
    py_ast.analyze(conn, str(repo), file_map)
    dataflow.analyze(conn, str(repo), file_map)
    yield conn
    conn.close()


def _feed_edges(conn):
    return conn.execute(
        """SELECT s.name, d.name, e.metadata_json
           FROM edge e
           JOIN edge_type t ON e.edge_type_id=t.id
           JOIN node s ON e.src_node_id=s.id
           JOIN node d ON e.dst_node_id=d.id
           WHERE t.name='downstream_data_feed'"""
    ).fetchall()


def test_simple_output_to_input(analyzed):
    pairs = {(s, d) for s, d, _ in _feed_edges(analyzed)}
    assert ("producer", "consumer") in pairs


def test_multiple_return_values_tracked(analyzed):
    pairs = {(s, d) for s, d, _ in _feed_edges(analyzed)}
    assert ("multi", "other") in pairs


def test_no_false_edge_for_unrelated_value(analyzed):
    # z = 5 (a literal) feeding consumer must NOT create a producer->consumer
    # edge originating from the literal; only real producer outputs count.
    pairs = [(s, d) for s, d, _ in _feed_edges(analyzed)]
    # consumer is fed by producer exactly once (from pipeline), not from unrelated
    assert pairs.count(("producer", "consumer")) == 1


def test_edge_metadata_records_variable(analyzed):
    rows = _feed_edges(analyzed)
    import json
    meta = [json.loads(m) for _, _, m in rows if m]
    assert any("var" in d for d in meta)
