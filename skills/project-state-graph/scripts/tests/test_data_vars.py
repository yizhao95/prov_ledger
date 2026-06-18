"""RED tests for analyzer.dataflow_types — data_var nodes + produces/consumes edges."""
import json

import pytest

from analyzer import store, walker, py_ast, dataflow_types


def _setup(tmp_path, source: str):
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "mod.py").write_text(source)
    path = str(tmp_path / "demo-state-graph.db")
    conn = store.init_db(path)
    fm = walker.walk(conn, str(repo))
    py_ast.analyze(conn, str(repo), fm)
    dataflow_types.analyze(conn, str(repo), fm)
    return conn


def _nodes_of_type(conn, type_name):
    return conn.execute(
        """SELECT n.id, n.name, n.metadata_json
           FROM node n JOIN node_type t ON n.node_type_id=t.id
           WHERE t.name=?""",
        (type_name,),
    ).fetchall()


def _edges_of_type(conn, type_name):
    return conn.execute(
        """SELECT e.src_node_id, e.dst_node_id, e.metadata_json
           FROM edge e JOIN edge_type t ON e.edge_type_id=t.id
           WHERE t.name=?""",
        (type_name,),
    ).fetchall()


def test_annotated_return_creates_typed_data_var(tmp_path):
    conn = _setup(tmp_path, "def f() -> list[int]:\n    return [1, 2]\n")
    dvs = _nodes_of_type(conn, "data_var")
    assert dvs, "expected a data_var node"
    types = {json.loads(m or "{}").get("type") for _, _, m in dvs}
    assert "list[int]" in types


def test_unannotated_returns_infer_shape(tmp_path):
    src = (
        "def tup():\n    a = 1\n    b = 2\n    return a, b\n\n"
        "def dct():\n    return {\"k\": 1}\n\n"
        "def lst():\n    return [1, 2, 3]\n"
    )
    conn = _setup(tmp_path, src)
    shapes = {json.loads(m or "{}").get("type") for _, _, m in _nodes_of_type(conn, "data_var")}
    assert "tuple[2]" in shapes
    assert "dict" in shapes
    assert "list" in shapes


def test_unknown_return_type(tmp_path):
    conn = _setup(tmp_path, "def f(x):\n    return x\n")
    shapes = {json.loads(m or "{}").get("type") for _, _, m in _nodes_of_type(conn, "data_var")}
    assert "unknown" in shapes


def test_produces_edge_function_to_data_var(tmp_path):
    conn = _setup(tmp_path, "def f() -> int:\n    return 1\n")
    produces = _edges_of_type(conn, "produces")
    assert produces, "expected a produces edge function->data_var"
    # src must be the function node
    fn_ids = {nid for nid, _, _ in _nodes_of_type(conn, "function")}
    dv_ids = {nid for nid, _, _ in _nodes_of_type(conn, "data_var")}
    assert any(s in fn_ids and d in dv_ids for s, d, _ in produces)


def test_consumes_edge_data_var_to_consumer_with_type(tmp_path):
    src = (
        "def producer() -> list[int]:\n    return [1]\n\n"
        "def consumer(x):\n    return x\n\n"
        "def main():\n    data = producer()\n    consumer(data)\n"
    )
    conn = _setup(tmp_path, src)
    consumes = _edges_of_type(conn, "consumes")
    assert consumes, "expected a consumes edge data_var->consumer"
    dv_ids = {nid for nid, _, _ in _nodes_of_type(conn, "data_var")}
    fn_by_name = {name: nid for nid, name, _ in _nodes_of_type(conn, "function")}
    assert any(
        s in dv_ids and d == fn_by_name["consumer"] for s, d, _ in consumes
    )
    # type carried on the consumes edge
    types = {json.loads(m or "{}").get("type") for _, _, m in consumes}
    assert "list[int]" in types


def test_edges_carry_confidence(tmp_path):
    conn = _setup(tmp_path, "def f() -> int:\n    return 1\n")
    produces = _edges_of_type(conn, "produces")
    confs = {json.loads(m or "{}").get("confidence") for _, _, m in produces}
    assert "high" in confs


# ---- total dtype coverage: inputs + intermediates also typed ----

def test_input_param_data_var_typed(tmp_path):
    conn = _setup(tmp_path, "def f(x: int, y: str) -> int:\n    return x\n")
    dvs = _nodes_of_type(conn, "data_var")
    names = {n for _, n, _ in dvs}
    # annotated parameters become typed data_var nodes named '<fn>:param:<name>'
    assert any(name.endswith(":param:x") for name in names)
    assert any(name.endswith(":param:y") for name in names)
    # the dtype is captured from the annotation with provenance
    by_name = {n: json.loads(m or "{}") for _, n, m in dvs}
    px = next(v for k, v in by_name.items() if k.endswith(":param:x"))
    assert px.get("dtype") == "int"
    assert px.get("dtype_provenance") == "annotation"


def test_return_data_var_has_dtype_provenance(tmp_path):
    conn = _setup(tmp_path, "def f() -> list[int]:\n    return [1]\n")
    dvs = _nodes_of_type(conn, "data_var")
    provs = {json.loads(m or "{}").get("dtype_provenance") for _, _, m in dvs}
    assert "annotation" in provs


def test_unknown_dtype_provenance(tmp_path):
    conn = _setup(tmp_path, "def f(x):\n    return x\n")
    dvs = _nodes_of_type(conn, "data_var")
    provs = {json.loads(m or "{}").get("dtype_provenance") for _, _, m in dvs}
    assert "unknown" in provs


# ---- consumes tracer coverage: stop false-positive 'isolated' data nodes ----

def _consumer_ids_for(conn):
    """Return {consumer_fn_name} set that have an incoming consumes edge."""
    consumes = _edges_of_type(conn, "consumes")
    fn_by_id = {nid: name for nid, name, _ in _nodes_of_type(conn, "function")}
    return {fn_by_id.get(d) for _, d, _ in consumes}


def test_consumes_via_attribute_call(tmp_path):
    """A value passed to obj.method(var) must register as consumed."""
    src = (
        "def producer() -> list[int]:\n    return [1]\n\n"
        "def consume_attr(x):\n    return x\n\n"
        "class Worker:\n"
        "    def consume_attr(self, x):\n        return x\n\n"
        "def main():\n"
        "    data = producer()\n"
        "    w = Worker()\n"
        "    w.consume_attr(data)\n"
    )
    conn = _setup(tmp_path, src)
    assert "consume_attr" in _consumer_ids_for(conn), (
        "value passed to an attribute call obj.method(var) was not traced"
    )


def test_consumes_inside_comprehension(tmp_path):
    """A value used as an arg inside a comprehension must register as consumed."""
    src = (
        "def producer() -> list[int]:\n    return [1]\n\n"
        "def transform(item):\n    return item\n\n"
        "def main():\n"
        "    data = producer()\n"
        "    return [transform(it) for it in data]\n"
    )
    conn = _setup(tmp_path, src)
    # 'data' is consumed by being iterated; 'transform' consumes each item.
    # At minimum, data feeding the comprehension must produce a consumes edge
    # to a callable referenced in that comprehension.
    assert _edges_of_type(conn, "consumes"), (
        "no consumes edge emitted for a value used in a comprehension"
    )


def test_consumes_value_passed_directly_in_comprehension_arg(tmp_path):
    """transform(data) called inside a comprehension over something else."""
    src = (
        "def producer() -> list[int]:\n    return [1]\n\n"
        "def transform(payload):\n    return payload\n\n"
        "def main():\n"
        "    data = producer()\n"
        "    return {k: transform(data) for k in range(3)}\n"
    )
    conn = _setup(tmp_path, src)
    assert "transform" in _consumer_ids_for(conn), (
        "value passed as a call arg inside a dict comprehension was not traced"
    )


def test_consumes_via_keyword_argument(tmp_path):
    """A value passed as a keyword arg func(key=var) must register as consumed."""
    src = (
        "def producer() -> list[int]:\n    return [1]\n\n"
        "def consume_kw(payload=None):\n    return payload\n\n"
        "def main():\n"
        "    data = producer()\n"
        "    consume_kw(payload=data)\n"
    )
    conn = _setup(tmp_path, src)
    assert "consume_kw" in _consumer_ids_for(conn), (
        "value passed as a keyword argument was not traced"
    )


# ---- param taint: a parameter that flows into a downstream call is data flow ----

def test_param_flowing_into_call_is_consumed(tmp_path):
    """A function parameter passed into another project call should create a
    consumes edge from the param data_var to that callable (so e.g. an
    onnx_path param feeding a model-load helper reads as real data flow)."""
    src = (
        "def load_model(path):\n    return path\n\n"
        "def init(onnx_path):\n"
        "    return load_model(onnx_path)\n"
    )
    conn = _setup(tmp_path, src)
    consumes = _edges_of_type(conn, "consumes")
    fn_by_id = {nid: name for nid, name, _ in _nodes_of_type(conn, "function")}
    dv_by_id = {nid: name for nid, name, _ in _nodes_of_type(conn, "data_var")}
    # expect: data_var 'init:param:onnx_path' --consumes--> load_model
    hit = any(
        dv_by_id.get(s) == "init:param:onnx_path"
        and fn_by_id.get(d) == "load_model"
        for s, d, _ in consumes
    )
    assert hit, "param onnx_path flowing into load_model() was not traced"
