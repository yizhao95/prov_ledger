"""RED tests for analyzer.de_overlay — data-engineering lineage chains.

Assembles upstream -> operation -> downstream lineage across the source nodes
that sql_refs / api_refs already produced. For a function that reads source(s)
and writes target(s), a `lineage` edge is drawn source -> target carrying the
function as the operation, so a DE pipeline's end-to-end flow is explicit.
"""
import json

import pytest

from analyzer import api_refs, de_overlay, py_ast, sql_refs, store, walker


def _analyze(tmp_path, source: str):
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "mod.py").write_text(source)
    conn = store.init_db(str(tmp_path / "demo-state-graph.db"))
    fm = walker.walk(conn, str(repo))
    py_ast.analyze(conn, str(repo), fm)
    sql_refs.analyze(conn, str(repo), fm)
    api_refs.analyze(conn, str(repo), fm)
    de_overlay.analyze(conn, str(repo), fm)
    return conn


def _edges(conn, type_name):
    return conn.execute(
        """SELECT s.name, d.name, e.metadata_json
           FROM edge e
           JOIN edge_type t ON e.edge_type_id=t.id
           JOIN node s ON e.src_node_id=s.id
           JOIN node d ON e.dst_node_id=d.id
           WHERE t.name=?""",
        (type_name,),
    ).fetchall()


def test_sql_to_sql_lineage(tmp_path):
    src = (
        "def etl():\n"
        '    r = "SELECT * FROM proj.ds.src"\n'
        "    read(r)\n"
        '    w = "INSERT INTO proj.ds.dst SELECT 1"\n'
        "    write(w)\n"
    )
    conn = _analyze(tmp_path, src)
    edges = _edges(conn, "lineage")
    pairs = {(s, d) for s, d, _ in edges}
    assert ("proj.ds.src", "proj.ds.dst") in pairs
    ops = {json.loads(m or "{}").get("op") for _, _, m in edges}
    assert "etl" in ops


def test_api_source_to_sql_lineage(tmp_path):
    src = (
        "import requests\n\n\n"
        "def ingest():\n"
        "    requests.get('https://api.example.com/feed')\n"
        '    w = "INSERT INTO proj.ds.raw SELECT 1"\n'
        "    write(w)\n"
    )
    conn = _analyze(tmp_path, src)
    pairs = {(s, d) for s, d, _ in _edges(conn, "lineage")}
    assert ("https://api.example.com/feed", "proj.ds.raw") in pairs


def test_no_lineage_without_a_write(tmp_path):
    src = (
        "def only_read():\n"
        '    r = "SELECT * FROM proj.ds.src"\n'
        "    read(r)\n"
    )
    conn = _analyze(tmp_path, src)
    assert _edges(conn, "lineage") == []
