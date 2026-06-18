"""RED tests for analyzer.store — the sole SQLite owner."""
import sqlite3

import pytest

from analyzer import store


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "demo-state-graph.db"
    conn = store.init_db(str(path))
    yield conn
    conn.close()


def _table_names(conn):
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    return {r[0] for r in rows}


def _columns(conn, table):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def test_init_db_creates_all_tables(db):
    assert {"node_type", "node", "edge_type", "edge", "analysis_run"} <= _table_names(db)


def test_node_table_has_expected_columns(db):
    cols = _columns(db, "node")
    assert {
        "id", "node_type_id", "name", "qualified_name",
        "file_path", "line_start", "line_end", "metadata_json",
    } <= cols


def test_edge_table_has_expected_columns(db):
    cols = _columns(db, "edge")
    assert {"id", "edge_type_id", "src_node_id", "dst_node_id", "metadata_json"} <= cols


def test_get_or_create_node_type_is_idempotent(db):
    a = store.get_or_create_node_type(db, "function")
    b = store.get_or_create_node_type(db, "function")
    assert a == b
    c = store.get_or_create_node_type(db, "class")
    assert c != a


def test_duplicate_node_type_name_uniqued(db):
    store.get_or_create_node_type(db, "file")
    store.get_or_create_node_type(db, "file")
    count = db.execute(
        "SELECT COUNT(*) FROM node_type WHERE name='file'"
    ).fetchone()[0]
    assert count == 1


def test_add_node_returns_rowid_with_location(db):
    tid = store.get_or_create_node_type(db, "function")
    nid = store.add_node(
        db, tid, name="build_query", qualified_name="bq.build_query",
        file_path="app/bq_signals.py", line_start=186, line_end=380,
    )
    assert isinstance(nid, int)
    row = db.execute(
        "SELECT name, file_path, line_start FROM node WHERE id=?", (nid,)
    ).fetchone()
    assert row == ("build_query", "app/bq_signals.py", 186)


def test_add_edge_links_two_nodes(db):
    ft = store.get_or_create_node_type(db, "function")
    n1 = store.add_node(db, ft, name="a", qualified_name="m.a", file_path="m.py")
    n2 = store.add_node(db, ft, name="b", qualified_name="m.b", file_path="m.py")
    et = store.get_or_create_edge_type(db, "calls")
    eid = store.add_edge(db, et, n1, n2, metadata={"line": 10})
    assert isinstance(eid, int)
    row = db.execute(
        "SELECT edge_type_id, src_node_id, dst_node_id FROM edge WHERE id=?", (eid,)
    ).fetchone()
    assert row == (et, n1, n2)


def test_indexes_exist(db):
    idx = {
        r[0]
        for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        ).fetchall()
    }
    # at least one index referencing each hot column
    sql = " ".join(
        r[0] or ""
        for r in db.execute(
            "SELECT sql FROM sqlite_master WHERE type='index'"
        ).fetchall()
    )
    assert "node_type_id" in sql
    assert "src_node_id" in sql
    assert "dst_node_id" in sql


def test_analysis_run_lifecycle(db):
    run_id = store.start_run(db, project_name="demo-app", commit_sha="abc123")
    assert isinstance(run_id, int)
    store.finish_run(db, run_id)
    row = db.execute(
        "SELECT project_name, finished_at FROM analysis_run WHERE id=?", (run_id,)
    ).fetchone()
    assert row[0] == "demo-app"
    assert row[1] is not None
