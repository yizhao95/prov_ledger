"""RED tests for analyzer.unresolved — conservative bare-name undefined-call detection.

A bare-name call (foo()) is flagged as an `unresolved_call` ONLY when the name is
not resolvable to: a project-defined callable, an imported symbol, a Python
builtin, or an in-scope local binding (param / assignment / nested def / class /
comprehension target / lambda arg). Attribute calls (obj.m(), self.x()) are never
flagged.
"""
from analyzer import py_ast, store, unresolved, walker


def _build(tmp_path, source: str):
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "mod.py").write_text(source)
    conn = store.init_db(str(tmp_path / "demo-state-graph.db"))
    file_map = walker.walk(conn, str(repo))
    py_ast.analyze(conn, str(repo), file_map)
    unresolved.analyze(conn, str(repo), file_map)
    return conn


def _unresolved_names(conn):
    rows = conn.execute(
        """SELECT n.name FROM node n JOIN node_type t ON n.node_type_id=t.id
           WHERE t.name='unresolved_call'"""
    ).fetchall()
    return [r[0] for r in rows]


def test_flags_genuinely_dangling_bare_call(tmp_path):
    src = (
        "def build_query():\n    return 1\n\n"
        "def main():\n    return bulid_query()\n"  # typo -> undefined
    )
    conn = _build(tmp_path, src)
    assert "bulid_query" in _unresolved_names(conn)
    conn.close()


def test_does_not_flag_builtin(tmp_path):
    src = "def main(xs):\n    return len(xs)\n"
    conn = _build(tmp_path, src)
    assert "len" not in _unresolved_names(conn)
    conn.close()


def test_does_not_flag_imported_symbol(tmp_path):
    src = (
        "from helpers import do_thing\n\n"
        "def main():\n    return do_thing()\n"
    )
    conn = _build(tmp_path, src)
    assert "do_thing" not in _unresolved_names(conn)
    conn.close()


def test_does_not_flag_local_variable_call(tmp_path):
    src = (
        "def main(cb):\n"
        "    handler = cb\n"
        "    return handler()\n"  # both 'cb' (param) and 'handler' (local) bound
    )
    conn = _build(tmp_path, src)
    names = _unresolved_names(conn)
    assert "handler" not in names
    assert "cb" not in names
    conn.close()


def test_does_not_flag_attribute_or_method_call(tmp_path):
    src = (
        "import json\n\n"
        "class A:\n"
        "    def run(self):\n"
        "        return self.helper()\n"
        "    def helper(self):\n"
        "        return json.dumps({})\n"
    )
    conn = _build(tmp_path, src)
    names = _unresolved_names(conn)
    # attribute calls are never bare names -> never flagged
    assert "helper" not in names
    assert "dumps" not in names
    assert "run" not in names
    conn.close()


def test_does_not_flag_project_defined_call(tmp_path):
    src = (
        "def helper():\n    return 1\n\n"
        "def main():\n    return helper()\n"
    )
    conn = _build(tmp_path, src)
    assert "helper" not in _unresolved_names(conn)
    conn.close()


def test_does_not_flag_nested_def(tmp_path):
    src = (
        "def outer():\n"
        "    def inner():\n"
        "        return 1\n"
        "    return inner()\n"
    )
    conn = _build(tmp_path, src)
    assert "inner" not in _unresolved_names(conn)
    conn.close()


def test_does_not_flag_comprehension_or_lambda_binding(tmp_path):
    src = (
        "def main(xs):\n"
        "    f = lambda y: y + 1\n"
        "    return [f(i) for i in xs]\n"
    )
    conn = _build(tmp_path, src)
    names = _unresolved_names(conn)
    assert "f" not in names
    conn.close()


def test_unresolved_node_records_file_and_line(tmp_path):
    src = (
        "def main():\n"
        "    return totally_missing()\n"
    )
    conn = _build(tmp_path, src)
    row = conn.execute(
        """SELECT n.name, n.file_path, n.line_start
           FROM node n JOIN node_type t ON n.node_type_id=t.id
           WHERE t.name='unresolved_call' AND n.name='totally_missing'"""
    ).fetchone()
    assert row is not None
    assert row[1].endswith("mod.py")
    assert row[2] == 2
    conn.close()
