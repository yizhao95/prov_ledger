"""Phase 3 (PSG-D1) — dtype/nullable/confidence as first-class indexed columns."""
import sqlite3

from analyzer import dataflow_types, py_ast, store, walker


def _cols(conn, table):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def test_schema_has_typed_columns_and_indexes(tmp_path):
    conn = store.init_db(str(tmp_path / "g.db"))
    assert {"dtype", "dtype_provenance", "data_class", "nullable"} <= _cols(conn, "node")
    assert "confidence" in _cols(conn, "edge")
    idx = {r[1] for r in conn.execute("PRAGMA index_list(node)")} | \
          {r[1] for r in conn.execute("PRAGMA index_list(edge)")}
    assert "idx_node_dtype" in idx and "idx_edge_confidence" in idx
    conn.close()


def test_add_node_mirrors_metadata_into_columns(tmp_path):
    conn = store.init_db(str(tmp_path / "g.db"))
    nt = store.get_or_create_node_type(conn, "data_var")
    nid = store.add_node(conn, nt, name="v", qualified_name="v",
                         metadata={"dtype": "int64", "dtype_provenance": "annotation",
                                   "data_class": "PII", "nullable": True})
    row = conn.execute(
        "SELECT dtype, dtype_provenance, data_class, nullable FROM node WHERE id=?",
        (nid,)).fetchone()
    conn.close()
    assert row == ("int64", "annotation", "PII", 1)  # bool nullable -> 1


def test_add_edge_mirrors_confidence(tmp_path):
    conn = store.init_db(str(tmp_path / "g.db"))
    nt = store.get_or_create_node_type(conn, "function")
    et = store.get_or_create_edge_type(conn, "calls")
    a = store.add_node(conn, nt, name="a", qualified_name="m.a")
    b = store.add_node(conn, nt, name="b", qualified_name="m.b")
    store.add_edge(conn, et, a, b, metadata={"confidence": "inferred"})
    conf = conn.execute("SELECT confidence FROM edge").fetchone()[0]
    conn.close()
    assert conf == "inferred"


def test_param_dtype_populated_end_to_end(tmp_path):
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "m.py").write_text("def f(x: int) -> str:\n    return 'a'\n")
    conn = store.init_db(str(tmp_path / "g.db"))
    fm = walker.walk(conn, str(repo))
    py_ast.analyze(conn, str(repo), fm)
    dataflow_types.analyze(conn, str(repo), fm)
    # the param data_var carries its dtype in the COLUMN (queryable, no json.loads)
    row = conn.execute(
        "SELECT dtype FROM node WHERE name LIKE '%:param:x' AND dtype IS NOT NULL"
    ).fetchone()
    conn.close()
    assert row is not None and row[0] == "int"


def test_defensive_alter_upgrades_old_db(tmp_path):
    # An old graph DB without the D1 columns must be upgraded by init_db.
    p = str(tmp_path / "old.db")
    c = sqlite3.connect(p)
    # realistic pre-D1 schema: has the original columns, lacks dtype/confidence
    c.executescript(
        "CREATE TABLE node (id INTEGER PRIMARY KEY, node_type_id INTEGER, name TEXT,"
        " qualified_name TEXT, file_path TEXT, line_start INTEGER, line_end INTEGER,"
        " metadata_json TEXT);"
        "CREATE TABLE edge (id INTEGER PRIMARY KEY, edge_type_id INTEGER,"
        " src_node_id INTEGER, dst_node_id INTEGER, metadata_json TEXT);")
    c.commit()
    c.close()
    conn = store.init_db(p)  # should ALTER in the missing columns, not crash
    assert {"dtype", "nullable"} <= _cols(conn, "node")
    assert "confidence" in _cols(conn, "edge")
    conn.close()
