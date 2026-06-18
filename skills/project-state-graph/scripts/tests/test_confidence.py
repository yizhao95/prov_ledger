"""RED tests for confidence tagging on calls and downstream_data_feed edges."""
import json

from analyzer import store, walker, py_ast, dataflow


def _setup(tmp_path, source: str):
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "mod.py").write_text(source)
    path = str(tmp_path / "demo-state-graph.db")
    conn = store.init_db(path)
    fm = walker.walk(conn, str(repo))
    py_ast.analyze(conn, str(repo), fm)
    dataflow.analyze(conn, str(repo), fm)
    return conn


def _edges_of_type(conn, type_name):
    return conn.execute(
        """SELECT e.src_node_id, e.dst_node_id, e.metadata_json
           FROM edge e JOIN edge_type t ON e.edge_type_id=t.id
           WHERE t.name=?""",
        (type_name,),
    ).fetchall()


def test_unique_call_is_high_confidence(tmp_path):
    src = (
        "def helper():\n    return 1\n\n"
        "def main():\n    return helper()\n"
    )
    conn = _setup(tmp_path, src)
    calls = _edges_of_type(conn, "calls")
    assert calls
    confs = {json.loads(m or "{}").get("confidence") for _, _, m in calls}
    assert "high" in confs


def test_ambiguous_name_call_is_inferred(tmp_path):
    # two functions named 'run' -> call resolution is ambiguous
    src = (
        "class A:\n    def run(self):\n        return 1\n\n"
        "class B:\n    def run(self):\n        return 2\n\n"
        "def main(obj):\n    return obj.run()\n"
    )
    conn = _setup(tmp_path, src)
    calls = _edges_of_type(conn, "calls")
    confs = {json.loads(m or "{}").get("confidence") for _, _, m in calls}
    assert "inferred" in confs


def test_downstream_data_feed_has_confidence(tmp_path):
    src = (
        "def producer():\n    return [1]\n\n"
        "def consumer(x):\n    return x\n\n"
        "def main():\n    data = producer()\n    consumer(data)\n"
    )
    conn = _setup(tmp_path, src)
    feeds = _edges_of_type(conn, "downstream_data_feed")
    assert feeds
    for _, _, m in feeds:
        assert json.loads(m or "{}").get("confidence") is not None
