"""Phase 2 follow-up — sql_refs/api_refs bind the enclosing function by module (PSG-C2)."""
from analyzer import api_refs, py_ast, sql_refs, store, walker


def _edges(conn, edge_name):
    return {
        (r[0], r[1]) for r in conn.execute(
            """SELECT s.qualified_name, d.name
               FROM edge e JOIN edge_type t ON t.id=e.edge_type_id
               JOIN node s ON s.id=e.src_node_id JOIN node d ON d.id=e.dst_node_id
               WHERE t.name=?""", (edge_name,)).fetchall()
    }


def test_sql_reads_bind_to_right_module(tmp_path):
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    # both modules define load(); only a.load reads the table
    (repo / "pkg" / "a.py").write_text(
        'def load():\n    q = "SELECT id FROM sales.daily"\n    return q\n')
    (repo / "pkg" / "b.py").write_text("def load():\n    return 2\n")
    conn = store.init_db(str(tmp_path / "g.db"))
    fm = walker.walk(conn, str(repo))
    py_ast.analyze(conn, str(repo), fm)
    sql_refs.analyze(conn, str(repo), fm)
    reads = _edges(conn, "reads_sql")
    conn.close()
    assert ("pkg.a.load", "sales.daily") in reads
    assert ("pkg.b.load", "sales.daily") not in reads


def test_api_reads_bind_to_right_module(tmp_path):
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "a.py").write_text(
        'import requests\n'
        'def fetch():\n    r = requests.get("https://api.x/v1/users")\n    return r["id"]\n')
    (repo / "pkg" / "b.py").write_text("def fetch():\n    return 0\n")
    conn = store.init_db(str(tmp_path / "g.db"))
    fm = walker.walk(conn, str(repo))
    py_ast.analyze(conn, str(repo), fm)
    api_refs.analyze(conn, str(repo), fm)
    reads = _edges(conn, "reads_api")
    conn.close()
    srcs = {s for s, _ in reads}
    assert "pkg.a.fetch" in srcs
    assert "pkg.b.fetch" not in srcs
