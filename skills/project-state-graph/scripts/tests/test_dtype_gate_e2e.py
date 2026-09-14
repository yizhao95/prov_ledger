"""PSG-C4: the e2e dtype gate must fire on a REAL producer/consumer mismatch.

Before the fix the gate compared a value to itself (both copied from the producer
type) and could never fire. Now consumes edges carry the consumer's declared param
type (expected_type), so a producer returning int feeding a consumer annotated str
is caught.
"""
from analyzer import dataflow_types, py_ast, store, walker
from selfcheck import _check_dtype_consistency_e2e


def _analyze(tmp_path, src):
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "flow.py").write_text(src)
    conn = store.init_db(str(tmp_path / "g.db"))
    file_map = walker.walk(conn, str(repo))
    py_ast.analyze(conn, str(repo), file_map)
    dataflow_types.analyze(conn, str(repo), file_map)
    return conn


MISMATCH = '''\
def produce() -> int:
    return 1


def consume(x: str):
    return x


def pipeline():
    v = produce()
    consume(v)
'''

MATCH = '''\
def produce() -> int:
    return 1


def consume(x: int):
    return x


def pipeline():
    v = produce()
    consume(v)
'''


def test_dtype_gate_fires_on_real_mismatch(tmp_path):
    conn = _analyze(tmp_path, MISMATCH)
    try:
        result = _check_dtype_consistency_e2e(conn)
        assert result["ok"] is False, "gate must fire: produced int consumed as str"
        assert "int" in result["detail"] and "str" in result["detail"]
    finally:
        conn.close()


def test_dtype_gate_clean_when_types_agree(tmp_path):
    conn = _analyze(tmp_path, MATCH)
    try:
        result = _check_dtype_consistency_e2e(conn)
        assert result["ok"] is True, f"gate should pass when types agree: {result}"
    finally:
        conn.close()


# ── FL-013: the gate must not fire on structurally-known non-breaks ──────────
# Evidence: on prov_ledger's own graph the gate reported 132 "breaks"; 82 were
# `inferred` edges (value flowed indirectly / ambiguous callee) and 50 were
# Optional-vs-concrete, generic refinement (list vs list[dict]) or tuple
# unpacking (`a, b = f()` bound BOTH names to f's whole return value).

def _analyze_files(tmp_path, files: dict):
    repo = tmp_path / "repo"
    for rel, src in files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(src)
    conn = store.init_db(str(tmp_path / "g.db"))
    file_map = walker.walk(conn, str(repo))
    py_ast.analyze(conn, str(repo), file_map)
    dataflow_types.analyze(conn, str(repo), file_map)
    return conn


def _gate(tmp_path, src):
    conn = _analyze(tmp_path, src)
    try:
        return _check_dtype_consistency_e2e(conn)
    finally:
        conn.close()


def test_optional_producer_into_concrete_consumer_is_not_a_break(tmp_path):
    src = '''\
from typing import Optional
def produce() -> Optional[int]:
    return 1
def consume(x: int):
    return x
def other() -> int:
    return 2
def take(y: "int | None"):
    return y
def pipeline():
    v = produce()
    consume(v)
    w = other()
    take(w)
'''
    r = _gate(tmp_path, src)
    assert r["ok"] is True, r["detail"]


def test_unparameterised_generic_accepts_refinement(tmp_path):
    src = '''\
def produce() -> list:
    return []
def consume(rows: list[dict]):
    return rows
def pipeline():
    v = produce()
    consume(v)
'''
    r = _gate(tmp_path, src)
    assert r["ok"] is True, r["detail"]


def test_tuple_unpacking_does_not_compare_whole_tuple_to_element(tmp_path):
    src = '''\
def produce() -> tuple[int, str]:
    return 1, "a"
def consume(x: str):
    return x
def pipeline():
    a, b = produce()
    consume(b)
'''
    conn = _analyze(tmp_path, src)
    try:
        r = _check_dtype_consistency_e2e(conn)
        assert r["ok"] is True, r["detail"]
        import json
        metas = [json.loads(m) for (m,) in conn.execute(
            "SELECT e.metadata_json FROM edge e JOIN edge_type t ON e.edge_type_id=t.id WHERE t.name='consumes'")]
        assert metas and all(m.get("unpacked") is True and m.get("type") == "unknown" for m in metas)
    finally:
        conn.close()


def test_inferred_edges_are_not_asserted(tmp_path):
    """Two same-named consumers -> both edges are `inferred`; a mismatch on one
    of them is a guess, not evidence, so the ERROR gate must stay quiet."""
    conn = _analyze_files(tmp_path, {
        "pkg/a.py": "def consume(x: str):\n    return x\n",
        "pkg/b.py": "def consume(x: int):\n    return x\n",
        "pkg/flow.py": "def produce() -> int:\n    return 1\ndef pipeline():\n    v = produce()\n    consume(v)\n",
    })
    try:
        r = _check_dtype_consistency_e2e(conn)
        assert r["ok"] is True, r["detail"]
    finally:
        conn.close()


def test_real_break_still_fires_after_relaxations(tmp_path):
    r = _gate(tmp_path, MISMATCH)
    assert r["ok"] is False and "int" in r["detail"] and "str" in r["detail"]


def test_fl029_bare_name_attribute_call_is_not_asserted(tmp_path):
    """FL-029: `d.get("k")` is a method call on a dict, not a call to the
    unrelated free function pkg.ns.get that happens to share the name. The
    builder may only guess (inferred) and the ERROR gate must not assert it."""
    conn = _analyze_files(tmp_path, {
        "pkg/ns.py": "def get(name: str) -> frozenset[str]:\n    return frozenset({name})\n",
        "pkg/use.py": ("def take(x: int) -> int:\n    return x\n\n"
                       "def flow(d: dict) -> int:\n    v = d.get('k')\n    return take(v)\n"),
    })
    try:
        r = _check_dtype_consistency_e2e(conn)
        assert r["ok"] is True, r["detail"]
        rows = conn.execute(
            """SELECT e.confidence FROM edge e JOIN edge_type t ON t.id=e.edge_type_id
               JOIN node s ON s.id=e.src_node_id JOIN node d ON d.id=e.dst_node_id
               WHERE t.name='calls' AND s.qualified_name='pkg.use.flow' AND d.qualified_name='pkg.ns.get'""").fetchall()
        assert all(c[0] == "inferred" for c in rows), rows       # never a high-confidence call
    finally:
        conn.close()
