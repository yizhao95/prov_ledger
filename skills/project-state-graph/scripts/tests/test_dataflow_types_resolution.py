"""Phase 2.3b — PSG-C3: each same-named function keeps its own produces/feeds."""
from analyzer import dataflow_types, py_ast, store, walker


def test_same_named_returning_functions_each_get_produces(tmp_path):
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "a.py").write_text("def make() -> int:\n    return 1\n")
    (repo / "pkg" / "b.py").write_text("def make() -> str:\n    return 'x'\n")
    conn = store.init_db(str(tmp_path / "g.db"))
    fm = walker.walk(conn, str(repo))
    py_ast.analyze(conn, str(repo), fm)
    dataflow_types.analyze(conn, str(repo), fm)

    rows = conn.execute(
        """SELECT s.qualified_name, e.metadata_json
           FROM edge e JOIN edge_type t ON t.id=e.edge_type_id
           JOIN node s ON s.id=e.src_node_id
           WHERE t.name='produces'""").fetchall()
    conn.close()
    srcs = {r[0] for r in rows}
    # BOTH same-named functions must have produced a data_var (C3: the 2nd no
    # longer collapses onto the 1st).
    assert "pkg.a.make" in srcs
    assert "pkg.b.make" in srcs
    assert len(rows) >= 2
