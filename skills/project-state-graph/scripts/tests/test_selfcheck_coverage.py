"""Tests for provLedger Phase C-2 — dtype coverage as a visible metric."""
from __future__ import annotations

import json
import sqlite3

import pytest

import selfcheck


def _mk(path, data_nodes):
    """data_nodes: list of (name, dtype_or_None). Builds a minimal graph."""
    c = sqlite3.connect(str(path))
    c.executescript(
        """
        CREATE TABLE node_type (id INTEGER PRIMARY KEY, name TEXT UNIQUE, description TEXT);
        CREATE TABLE node (id INTEGER PRIMARY KEY, node_type_id INTEGER, name TEXT,
                           qualified_name TEXT, file_path TEXT, line_start INTEGER,
                           line_end INTEGER, metadata_json TEXT, dtype TEXT);
        CREATE TABLE edge_type (id INTEGER PRIMARY KEY, name TEXT UNIQUE, description TEXT);
        CREATE TABLE edge (id INTEGER PRIMARY KEY, edge_type_id INTEGER, src_node_id INTEGER,
                           dst_node_id INTEGER, metadata_json TEXT);
        CREATE TABLE analysis_run (id INTEGER PRIMARY KEY, commit_sha TEXT);
        INSERT INTO node_type (name) VALUES ('data_var');
        INSERT INTO analysis_run (commit_sha) VALUES ('deadbeef');
        """
    )
    for name, dtype in data_nodes:
        meta = json.dumps({"dtype": dtype}) if dtype is not None else None
        c.execute(
            "INSERT INTO node (node_type_id,name,qualified_name,file_path,metadata_json,dtype) "
            "VALUES (1,?,?,?,?,?)", (name, name, "m.py", meta, dtype))
    c.commit()
    c.close()


@pytest.fixture
def mixed_db(tmp_path):
    p = tmp_path / "g.db"
    _mk(p, [("a", "int"), ("b", "DataFrame"), ("c", "unknown"), ("d", None)])
    return str(p)


@pytest.fixture
def empty_db(tmp_path):
    p = tmp_path / "e.db"
    _mk(p, [])
    return str(p)


# ── dtype_coverage() helper ──────────────────────────────────────────────────────

def test_coverage_counts(mixed_db):
    conn = sqlite3.connect(mixed_db)
    try:
        cov = selfcheck.dtype_coverage(conn)
    finally:
        conn.close()
    assert cov["typed"] == 2     # int, DataFrame
    assert cov["unknown"] == 2   # 'unknown', missing
    assert cov["total"] == 4
    assert cov["pct"] == 50.0


def test_coverage_empty_no_zero_division(empty_db):
    conn = sqlite3.connect(empty_db)
    try:
        cov = selfcheck.dtype_coverage(conn)
    finally:
        conn.close()
    assert cov == {"typed": 0, "unknown": 0, "total": 0, "pct": 0.0}


# ── run() surfaces the coverage line ─────────────────────────────────────────────

def test_run_includes_dtype_coverage_check(mixed_db):
    res = selfcheck.run(mixed_db)
    cov = next((c for c in res["checks"] if c["name"] == "dtype_coverage"), None)
    assert cov is not None
    assert cov["severity"] == "warning"
    assert "%" in cov["detail"]
    assert "coverage" in cov["detail"].lower()


def test_run_report_has_coverage_line(mixed_db):
    res = selfcheck.run(mixed_db)
    assert "dtype coverage:" in res["report"].lower()
    assert "50.0%" in res["report"]


def test_run_coverage_never_flips_ok_even_at_zero(empty_db):
    # empty data surface -> 0% coverage, but build must still be able to pass
    res = selfcheck.run(empty_db)
    cov = next(c for c in res["checks"] if c["name"] == "dtype_coverage")
    assert cov["severity"] == "warning"
    # coverage check itself must not be an error-severity blocker
    assert cov.get("severity") != "error"
