"""RED tests for analyzer.pipeline — pipeline nodes + ordered pipeline_step edges."""
import json

import pytest

from analyzer import pipeline, py_ast, store, walker

SAMPLE = '''\
def step_one():
    return 1


def step_two():
    return 2


def step_three():
    return 3


def run_all():
    step_one()
    step_two()
    step_three()


if __name__ == "__main__":
    step_one()
    step_two()
    step_three()
'''


@pytest.fixture
def analyzed(tmp_path):
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "orch.py").write_text(SAMPLE)
    conn = store.init_db(str(tmp_path / "demo-state-graph.db"))
    file_map = walker.walk(conn, str(repo))
    py_ast.analyze(conn, str(repo), file_map)
    pipeline.analyze(conn, str(repo), file_map)
    yield conn
    conn.close()


def _pipeline_nodes(conn):
    return conn.execute(
        """SELECT n.name FROM node n JOIN node_type t ON n.node_type_id=t.id
           WHERE t.name='pipeline'"""
    ).fetchall()


def _step_edges(conn):
    return conn.execute(
        """SELECT s.name, d.name, e.metadata_json
           FROM edge e
           JOIN edge_type t ON e.edge_type_id=t.id
           JOIN node s ON e.src_node_id=s.id
           JOIN node d ON e.dst_node_id=d.id
           WHERE t.name='pipeline_step'"""
    ).fetchall()


def test_main_block_creates_pipeline_node(analyzed):
    names = {r[0] for r in _pipeline_nodes(analyzed)}
    assert any("main" in n for n in names)


def test_orchestrator_function_detected_as_pipeline(analyzed):
    names = {r[0] for r in _pipeline_nodes(analyzed)}
    assert any("run_all" in n for n in names)


def test_ordered_pipeline_steps(analyzed):
    edges = _step_edges(analyzed)
    pairs = {(s, d) for s, d, _ in edges}
    assert ("step_one", "step_two") in pairs
    assert ("step_two", "step_three") in pairs


def test_step_index_in_metadata(analyzed):
    metas = [json.loads(m) for _, _, m in _step_edges(analyzed) if m]
    assert any("index" in d for d in metas)
