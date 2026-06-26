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
