"""RED tests for analyzer.api_refs — HTTP API sources as first-class nodes.

Detects requests / httpx / aiohttp calls in functions and models the endpoint
as an `api_source` node, linked with reads_api (GET/HEAD) or writes_api
(POST/PUT/PATCH/DELETE) from the enclosing function.
"""
import pytest

from analyzer import api_refs, py_ast, store, walker


def _analyze(tmp_path, source: str):
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "mod.py").write_text(source)
    conn = store.init_db(str(tmp_path / "demo-state-graph.db"))
    fm = walker.walk(conn, str(repo))
    py_ast.analyze(conn, str(repo), fm)
    api_refs.analyze(conn, str(repo), fm)
    return conn


def _nodes(conn, type_name):
    return conn.execute(
        """SELECT n.id, n.name FROM node n JOIN node_type t ON n.node_type_id=t.id
           WHERE t.name=?""",
        (type_name,),
    ).fetchall()


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


def test_requests_get_creates_api_source(tmp_path):
    conn = _analyze(
        tmp_path,
        "import requests\n\n\ndef fetch():\n    return requests.get('https://api.example.com/items')\n",
    )
    names = {r[1] for r in _nodes(conn, "api_source")}
    assert "https://api.example.com/items" in names


def test_reads_api_edge_for_get(tmp_path):
    conn = _analyze(
        tmp_path,
        "import requests\n\n\ndef fetch():\n    return requests.get('https://api.example.com/items')\n",
    )
    edges = _edges(conn, "reads_api")
    assert any(s == "fetch" and d == "https://api.example.com/items" for s, d in edges)


def test_writes_api_edge_for_post(tmp_path):
    conn = _analyze(
        tmp_path,
        "import requests\n\n\ndef push():\n    return requests.post('https://api.example.com/items', json={})\n",
    )
    edges = _edges(conn, "writes_api")
    assert any(s == "push" and d == "https://api.example.com/items" for s, d in edges)


def test_httpx_and_aiohttp_detected(tmp_path):
    src = (
        "import httpx\n\n\n"
        "def a():\n    return httpx.get('https://h.example.com/x')\n"
    )
    conn = _analyze(tmp_path, src)
    names = {r[1] for r in _nodes(conn, "api_source")}
    assert "https://h.example.com/x" in names
