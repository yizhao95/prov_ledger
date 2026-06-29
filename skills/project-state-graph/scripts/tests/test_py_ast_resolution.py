"""Phase 2 — py_ast global call resolution: cross-module (#8) + self-call class (#4)."""
from analyzer import py_ast, store, walker


def _calls(conn):
    return {
        (r[0], r[1]) for r in conn.execute(
            """SELECT s.qualified_name, d.qualified_name
               FROM edge e JOIN edge_type t ON t.id=e.edge_type_id
               JOIN node s ON s.id=e.src_node_id JOIN node d ON d.id=e.dst_node_id
               WHERE t.name='calls'""").fetchall()
    }


def _build(tmp_path, files: dict):
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    for name, src in files.items():
        (repo / "pkg" / name).write_text(src)
    conn = store.init_db(str(tmp_path / "g.db"))
    fm = walker.walk(conn, str(repo))
    py_ast.analyze(conn, str(repo), fm)
    return conn


def test_cross_module_call_resolves(tmp_path):
    # #8: a call to an imported function now produces a calls edge to the
    # right module's function (old by_name was module-local -> edge dropped).
    conn = _build(tmp_path, {
        "a.py": "from pkg.b import helper\n\ndef run():\n    helper()\n",
        "b.py": "def helper():\n    return 1\n",
    })
    calls = _calls(conn)
    conn.close()
    assert ("pkg.a.run", "pkg.b.helper") in calls


def test_self_call_binds_to_method_not_toplevel(tmp_path):
    # #4: self.foo() inside class C binds to C.foo (the method), not a same-named
    # top-level function.
    conn = _build(tmp_path, {
        "m.py": (
            "def foo():\n    return 0\n\n"
            "class C:\n"
            "    def foo(self):\n        return 1\n\n"
            "    def bar(self):\n        return self.foo()\n"
        ),
    })
    calls = _calls(conn)
    conn.close()
    assert ("pkg.m.C.bar", "pkg.m.C.foo") in calls
    assert ("pkg.m.C.bar", "pkg.m.foo") not in calls  # NOT the top-level foo
