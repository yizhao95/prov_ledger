"""RED tests for analyzer.profiles — sub-flow profile tags on app symbols.

A `profile` node is created per distinct sub-flow name and `tagged_profile`
edges link function/method nodes to the profiles they belong to. A single
symbol may carry MULTIPLE profile tags. Profiles are derived purely from the
graph that the other analyzers already produced (plus a conservative AST scan
for ML call patterns).

Sub-flows:
  function-calling   - participates in calls edges, none of the richer flows
  pipeline           - is a step inside a pipeline (contains_step target)
  data-flow          - produces/consumes a data_var
  ml-training        - calls train_test_split / .fit / .train
  data-engineering   - both reads_sql and writes_sql
"""
import pytest

from analyzer import (
    dataflow_types,
    pipeline,
    profiles,
    py_ast,
    sql_refs,
    store,
    walker,
)


def _analyze(tmp_path, source: str):
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "mod.py").write_text(source)
    conn = store.init_db(str(tmp_path / "demo-state-graph.db"))
    fm = walker.walk(conn, str(repo))
    py_ast.analyze(conn, str(repo), fm)
    dataflow_types.analyze(conn, str(repo), fm)
    pipeline.analyze(conn, str(repo), fm)
    sql_refs.analyze(conn, str(repo), fm)
    profiles.analyze(conn, str(repo), fm)
    return conn


def _profile_tags(conn):
    """Return {symbol_name: {profile_name, ...}} from tagged_profile edges."""
    rows = conn.execute(
        """SELECT s.name, p.name
           FROM edge e
           JOIN edge_type t ON e.edge_type_id=t.id
           JOIN node s ON e.src_node_id=s.id
           JOIN node p ON e.dst_node_id=p.id
           WHERE t.name='tagged_profile'"""
    ).fetchall()
    out: dict = {}
    for sym, prof in rows:
        out.setdefault(sym, set()).add(prof)
    return out


def _profile_nodes(conn):
    return {
        r[0]
        for r in conn.execute(
            """SELECT n.name FROM node n JOIN node_type t ON n.node_type_id=t.id
               WHERE t.name='profile'"""
        ).fetchall()
    }


def test_profile_nodes_created(tmp_path):
    conn = _analyze(
        tmp_path,
        "def a():\n    return 1\n\n\ndef b():\n    a()\n",
    )
    assert "function-calling" in _profile_nodes(conn)


def test_function_calling_tag(tmp_path):
    conn = _analyze(
        tmp_path,
        "def a():\n    return 1\n\n\ndef b():\n    a()\n",
    )
    tags = _profile_tags(conn)
    assert "function-calling" in tags.get("b", set())


def test_pipeline_tag(tmp_path):
    src = (
        "def s1():\n    return 1\n\n\n"
        "def s2():\n    return 2\n\n\n"
        "def run():\n    s1()\n    s2()\n"
    )
    conn = _analyze(tmp_path, src)
    tags = _profile_tags(conn)
    assert "pipeline" in tags.get("s1", set())
    assert "pipeline" in tags.get("s2", set())


def test_data_flow_tag(tmp_path):
    src = (
        "def producer() -> list[int]:\n    return [1]\n\n\n"
        "def consumer(x):\n    return x\n\n\n"
        "def main():\n    data = producer()\n    consumer(data)\n"
    )
    conn = _analyze(tmp_path, src)
    tags = _profile_tags(conn)
    assert "data-flow" in tags.get("producer", set())
    assert "data-flow" in tags.get("consumer", set())


def test_ml_training_tag(tmp_path):
    src = (
        "def train():\n"
        "    X_train, X_test = train_test_split(data)\n"
        "    model.fit(X_train)\n"
    )
    conn = _analyze(tmp_path, src)
    tags = _profile_tags(conn)
    assert "ml-training" in tags.get("train", set())


def test_data_engineering_tag(tmp_path):
    src = (
        "def etl():\n"
        '    q = "SELECT * FROM proj.ds.src"\n'
        "    read(q)\n"
        '    w = "INSERT INTO proj.ds.dst SELECT 1"\n'
        "    write(w)\n"
    )
    conn = _analyze(tmp_path, src)
    tags = _profile_tags(conn)
    assert "data-engineering" in tags.get("etl", set())


def test_multiple_tags_on_one_symbol(tmp_path):
    # produces a data_var AND is a pipeline step -> data-flow + pipeline
    src = (
        "def producer() -> list[int]:\n    return [1]\n\n\n"
        "def consumer(x):\n    return x\n\n\n"
        "def run():\n    d = producer()\n    consumer(d)\n"
    )
    conn = _analyze(tmp_path, src)
    tags = _profile_tags(conn)
    assert {"data-flow", "pipeline"} <= tags.get("producer", set())


# ---- A: data-flow requires REAL flow, not just 'returns something' ----

def test_unconsumed_return_is_not_data_flow(tmp_path):
    """A function that returns a value nobody consumes must NOT be tagged
    data-flow just for having a return (the old false-positive)."""
    src = (
        "def lonely() -> int:\n    return 1\n\n\n"
        "def caller():\n    lonely()\n"
    )
    conn = _analyze(tmp_path, src)
    tags = _profile_tags(conn)
    assert "data-flow" not in tags.get("lonely", set()), (
        "function whose return is never consumed should not be data-flow"
    )


def test_consumed_return_is_data_flow(tmp_path):
    src = (
        "def producer() -> list[int]:\n    return [1]\n\n\n"
        "def sink(x):\n    return x\n\n\n"
        "def main():\n    d = producer()\n    sink(d)\n"
    )
    conn = _analyze(tmp_path, src)
    tags = _profile_tags(conn)
    assert "data-flow" in tags.get("producer", set())
    assert "data-flow" in tags.get("sink", set())


# ---- B: route handlers are endpoints, not data ----

def _analyze_with_routes(tmp_path, source: str):
    from analyzer import routes
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "mod.py").write_text(source)
    conn = store.init_db(str(tmp_path / "demo-state-graph.db"))
    fm = walker.walk(conn, str(repo))
    py_ast.analyze(conn, str(repo), fm)
    dataflow_types.analyze(conn, str(repo), fm)
    pipeline.analyze(conn, str(repo), fm)
    sql_refs.analyze(conn, str(repo), fm)
    routes.analyze(conn, str(repo), fm)
    profiles.analyze(conn, str(repo), fm)
    return conn


def test_route_handler_tagged_endpoint(tmp_path):
    src = (
        "from fastapi import FastAPI\n"
        "app = FastAPI()\n\n\n"
        '@app.get("/healthz")\n'
        "def healthz():\n    return {\"status\": \"ok\"}\n"
    )
    conn = _analyze_with_routes(tmp_path, src)
    tags = _profile_tags(conn)
    assert "endpoint" in tags.get("healthz", set()), (
        "a route handler should carry the endpoint profile"
    )


def test_route_handler_not_data_flow(tmp_path):
    src = (
        "from fastapi import FastAPI\n"
        "app = FastAPI()\n\n\n"
        '@app.get("/healthz")\n'
        "def healthz():\n    return {\"status\": \"ok\"}\n"
    )
    conn = _analyze_with_routes(tmp_path, src)
    tags = _profile_tags(conn)
    assert "data-flow" not in tags.get("healthz", set()), (
        "a route handler whose return goes to the framework is not data-flow"
    )


def test_endpoint_is_a_profile_node(tmp_path):
    src = (
        "from fastapi import FastAPI\n"
        "app = FastAPI()\n\n\n"
        '@app.get("/x")\n'
        "def handler():\n    return 1\n"
    )
    conn = _analyze_with_routes(tmp_path, src)
    assert "endpoint" in _profile_nodes(conn)
