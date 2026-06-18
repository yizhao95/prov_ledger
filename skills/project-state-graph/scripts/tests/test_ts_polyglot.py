"""RED tests for analyzer.ts_polyglot — tree-sitter JS/HTML/CSS graph."""
import json

import pytest

from analyzer import store, ts_polyglot, walker

JS_SAMPLE = """\
function helper(x) {
  return x + 1;
}

const main = () => {
  return helper(2);
};

function init() {
  main();
}
"""

HTML_SAMPLE = """\
<html>
  <head>
    <link rel="stylesheet" href="style.css">
    <script src="ui.js"></script>
  </head>
  <body>
    <div class="card" id="main"></div>
  </body>
</html>
"""

CSS_SAMPLE = """\
.card { color: red; }
#main { width: 10px; }
"""


@pytest.fixture
def analyzed(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "ui.js").write_text(JS_SAMPLE)
    (repo / "page.html").write_text(HTML_SAMPLE)
    (repo / "style.css").write_text(CSS_SAMPLE)
    conn = store.init_db(str(tmp_path / "demo-state-graph.db"))
    file_map = walker.walk(conn, str(repo))
    ts_polyglot.analyze(conn, str(repo), file_map)
    yield conn
    conn.close()


def _node_names(conn, type_name):
    return {
        r[0]
        for r in conn.execute(
            """SELECT n.name FROM node n JOIN node_type t ON n.node_type_id=t.id
               WHERE t.name=?""",
            (type_name,),
        ).fetchall()
    }


def _edges(conn, type_name):
    return conn.execute(
        """SELECT s.name, d.name
           FROM edge e
           JOIN edge_type t ON e.edge_type_id=t.id
           JOIN node s ON e.src_node_id=s.id
           JOIN node d ON e.dst_node_id=d.id
           WHERE t.name=?""",
        (type_name,),
    ).fetchall()


def test_js_functions_become_nodes(analyzed):
    funcs = _node_names(analyzed, "function")
    assert "helper" in funcs
    assert "main" in funcs   # arrow function assigned to const
    assert "init" in funcs


def test_js_call_edges(analyzed):
    calls = {(s, d) for s, d in _edges(analyzed, "calls")}
    assert ("main", "helper") in calls
    assert ("init", "main") in calls


def test_html_references_js_and_css(analyzed):
    refs = {(s, d) for s, d in _edges(analyzed, "references")}
    # page.html references ui.js and style.css
    assert any(s == "page.html" and d == "ui.js" for s, d in refs)
    assert any(s == "page.html" and d == "style.css" for s, d in refs)


def test_css_selectors_become_nodes(analyzed):
    sels = _node_names(analyzed, "css_selector")
    assert ".card" in sels
    assert "#main" in sels


def test_html_references_css_selectors(analyzed):
    refs = {(s, d) for s, d in _edges(analyzed, "references")}
    # class="card" -> .card ; id="main" -> #main
    assert any(d == ".card" for _, d in refs)
    assert any(d == "#main" for _, d in refs)
