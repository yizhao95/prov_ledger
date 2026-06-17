"""A-4 tests — stored assumed-schema on sql_table / api_source nodes.

provLedger Phase A-4: make the upstream-data assumption explicit. When code
reads a source (a SQL table via a SELECT, or an HTTP API), the analyzer records
the columns the code EXPECTS that source to return as an 'assumed_schema'
attribute in the node's metadata_json. This is the data structure the Phase D
pre-flight warning reads. Purely additive — no schema migration (generic graph).
"""
import json

import pytest

import selfcheck
from analyzer import api_refs, py_ast, sql_refs, store, walker


def _node_meta(conn, type_name, node_name):
    row = conn.execute(
        """SELECT n.metadata_json FROM node n JOIN node_type t ON n.node_type_id=t.id
           WHERE t.name=? AND n.name=?""",
        (type_name, node_name),
    ).fetchone()
    return json.loads(row[0]) if row and row[0] else {}


# ── A-4 · SQL table assumed schema ───────────────────────────────────────────────

SQL_PY = '''\
def load_sales():
    return run("SELECT store_id, amount, ts FROM `wmt.retail.sales`")
'''


@pytest.fixture
def sql_analyzed(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "loader.py").write_text(SQL_PY)
    conn = store.init_db(str(tmp_path / "g.db"))
    file_map = walker.walk(conn, str(repo))
    py_ast.analyze(conn, str(repo), file_map)
    sql_refs.analyze(conn, str(repo), file_map)
    yield conn
    conn.close()


def test_sql_table_gains_assumed_schema(sql_analyzed):
    meta = _node_meta(sql_analyzed, "bq_dataset", "wmt.retail.sales")
    assert "assumed_schema" in meta
    cols = meta["assumed_schema"]
    # assumed_schema is {column: dtype}; dtype may be 'unknown' from a bare SELECT
    assert set(cols) >= {"store_id", "amount", "ts"}


def test_assumed_schema_absent_is_tolerated(tmp_path):
    # a SELECT * gives no concrete columns -> assumed_schema may be absent/empty,
    # and that must not break the build.
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "loader.py").write_text(
        'def load():\n    return run("SELECT * FROM `wmt.retail.x`")\n'
    )
    conn = store.init_db(str(tmp_path / "g.db"))
    file_map = walker.walk(conn, str(repo))
    py_ast.analyze(conn, str(repo), file_map)
    sql_refs.analyze(conn, str(repo), file_map)
    meta = _node_meta(conn, "bq_dataset", "wmt.retail.x")
    # either no assumed_schema, or it's just {'*': ...}; both are acceptable
    assert meta.get("assumed_schema", {}) in ({}, {"*": "unknown"}) or "*" in meta.get("assumed_schema", {})
    conn.close()


# ── A-4 · API source assumed schema ──────────────────────────────────────────────

API_PY = '''\
import requests

def fetch_user():
    resp = requests.get("https://api.example.com/user")
    data = resp.json()
    return data["name"], data["email"]
'''


@pytest.fixture
def api_analyzed(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "client.py").write_text(API_PY)
    conn = store.init_db(str(tmp_path / "g.db"))
    file_map = walker.walk(conn, str(repo))
    py_ast.analyze(conn, str(repo), file_map)
    api_refs.analyze(conn, str(repo), file_map)
    yield conn
    conn.close()


def test_api_source_gains_assumed_schema(api_analyzed):
    meta = _node_meta(api_analyzed, "api_source", "https://api.example.com/user")
    assert "assumed_schema" in meta
    # the code subscripts the response with 'name' and 'email'
    assert set(meta["assumed_schema"]) >= {"name", "email"}


# ── A-4 · selfcheck still passes with the new attribute ──────────────────────────

def test_selfcheck_passes_with_assumed_schema(sql_analyzed):
    # selfcheck must not error on the new metadata attribute
    result = selfcheck.run_on_conn(sql_analyzed) if hasattr(selfcheck, "run_on_conn") else None
    if result is None:
        pytest.skip("selfcheck has no run_on_conn helper; covered by init_project e2e")
    errors = [c for c in result if getattr(c, "severity", "") == "error" and not c.ok]
    assert errors == []
