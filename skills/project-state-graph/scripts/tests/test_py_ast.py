"""RED tests for analyzer.py_ast — Python function/class/import/call extraction."""
import pytest

from analyzer import py_ast, store, walker

SAMPLE = '''\
import os
from collections import defaultdict


def helper(x):
    return x + 1


def main():
    y = helper(1)
    return y


class Widget:
    def render(self):
        return helper(2)

    def size(self):
        return 0
'''


@pytest.fixture
def analyzed(tmp_path):
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "mod.py").write_text(SAMPLE)
    conn = store.init_db(str(tmp_path / "demo-state-graph.db"))
    file_map = walker.walk(conn, str(repo))
    py_ast.analyze(conn, str(repo), file_map)
    yield conn
    conn.close()


def _nodes_of(conn, type_name):
    return conn.execute(
        """SELECT n.name, n.qualified_name, n.line_start, n.line_end
           FROM node n JOIN node_type t ON n.node_type_id=t.id
           WHERE t.name=?""",
        (type_name,),
    ).fetchall()


def _edges_of(conn, type_name):
    return conn.execute(
        """SELECT s.name, d.name
           FROM edge e
           JOIN edge_type t ON e.edge_type_id=t.id
           JOIN node s ON e.src_node_id=s.id
           JOIN node d ON e.dst_node_id=d.id
           WHERE t.name=?""",
        (type_name,),
    ).fetchall()


def test_functions_become_nodes_with_lines(analyzed):
    funcs = {r[0]: r for r in _nodes_of(analyzed, "function")}
    assert "helper" in funcs
    assert "main" in funcs
    assert funcs["helper"][2] >= 1          # line_start set
    assert funcs["helper"][3] >= funcs["helper"][2]  # line_end >= line_start


def test_classes_and_methods(analyzed):
    classes = {r[0] for r in _nodes_of(analyzed, "class")}
    methods = {r[0] for r in _nodes_of(analyzed, "method")}
    assert "Widget" in classes
    assert "render" in methods
    assert "size" in methods


def test_class_defines_method_edges(analyzed):
    defines = _edges_of(analyzed, "defines")
    assert ("Widget", "render") in defines
    assert ("Widget", "size") in defines


def test_file_defines_function_edges(analyzed):
    defines = _edges_of(analyzed, "defines")
    assert ("mod.py", "helper") in defines
    assert ("mod.py", "main") in defines


def test_import_edges(analyzed):
    imports = _edges_of(analyzed, "imports")
    targets = {d for _, d in imports}
    assert "os" in targets
    assert "collections" in targets


def test_intra_file_call_edges(analyzed):
    calls = _edges_of(analyzed, "calls")
    assert ("main", "helper") in calls
    assert ("render", "helper") in calls


def test_property_attribute_read_counts_as_call(tmp_path):
    """Reading a @property as obj.prop (no parens) should register a calls edge,
    so properties don't look 'never called'."""
    src = (
        "class Model:\n"
        "    @property\n"
        "    def model_version(self) -> str:\n"
        "        return self._v\n\n\n"
        "def report(m):\n"
        "    return m.model_version\n"
    )
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "mod.py").write_text(src)
    conn = store.init_db(str(tmp_path / "demo-state-graph.db"))
    fm = walker.walk(conn, str(repo))
    py_ast.analyze(conn, str(repo), fm)
    calls = _edges_of(conn, "calls")
    conn.close()
    assert ("report", "model_version") in calls, (
        "a @property read obj.prop should be recorded as a call"
    )


def test_plain_attribute_read_is_not_a_call(tmp_path):
    """A non-property attribute read must NOT create a spurious calls edge."""
    src = (
        "class Box:\n"
        "    def width(self):\n"
        "        return 1\n\n\n"
        "def use(b):\n"
        "    return b.width\n"  # method referenced, not a @property, not called
    )
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "mod.py").write_text(src)
    conn = store.init_db(str(tmp_path / "demo-state-graph.db"))
    fm = walker.walk(conn, str(repo))
    py_ast.analyze(conn, str(repo), fm)
    calls = _edges_of(conn, "calls")
    conn.close()
    assert ("use", "width") not in calls


# --- import-symbol tracking (for the unresolved-symbol resolver) ---

def test_imported_symbols_tracks_from_import_names():
    src = "from collections import defaultdict, OrderedDict\n"
    syms = py_ast.imported_symbols(src)
    assert "defaultdict" in syms
    assert "OrderedDict" in syms


def test_imported_symbols_tracks_plain_import_top_module():
    src = "import os\nimport os.path\n"
    syms = py_ast.imported_symbols(src)
    assert "os" in syms


def test_imported_symbols_respects_asname():
    src = "from foo import bar as baz\nimport numpy as np\n"
    syms = py_ast.imported_symbols(src)
    assert "baz" in syms
    assert "bar" not in syms
    assert "np" in syms


def test_imported_symbols_empty_when_no_imports():
    assert py_ast.imported_symbols("def f():\n    return 1\n") == set()
