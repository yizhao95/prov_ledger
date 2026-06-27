"""RED tests for analyzer.cli — end-to-end orchestration into [project]-state-graph.db."""
import os
import sqlite3
import subprocess
import sys

import pytest

REPO_FIXTURE = '''\
from fastapi import FastAPI

app = FastAPI()


def helper():
    return 1


@app.get("/")
def index():
    return helper()


@app.post("/refresh")
def refresh():
    return 2
'''


def _make_repo(tmp_path):
    repo = tmp_path / "repo"
    (repo / "app").mkdir(parents=True)
    (repo / "app" / "main.py").write_text(REPO_FIXTURE)
    return repo


def _run_cli(repo, *args):
    env = dict(os.environ)
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env["PYTHONPATH"] = root
    return subprocess.run(
        [sys.executable, "-m", "analyzer", str(repo), *args],
        capture_output=True, text=True, cwd=root, env=env,
    )


def test_creates_named_db(tmp_path):
    repo = _make_repo(tmp_path)
    db = tmp_path / "out" / "demo-state-graph.db"
    db.parent.mkdir()
    r = _run_cli(repo, "--project", "demo", "--db-path", str(db))
    assert r.returncode == 0, r.stderr
    assert db.exists()


def test_records_analysis_run(tmp_path):
    repo = _make_repo(tmp_path)
    db = tmp_path / "demo-state-graph.db"
    _run_cli(repo, "--project", "demo", "--db-path", str(db))
    conn = sqlite3.connect(str(db))
    row = conn.execute(
        "SELECT project_name, started_at, finished_at FROM analysis_run"
    ).fetchone()
    conn.close()
    assert row[0] == "demo"
    assert row[1] is not None and row[2] is not None


def test_default_db_name_uses_project(tmp_path):
    repo = _make_repo(tmp_path)
    out = tmp_path / "out"
    out.mkdir()
    r = _run_cli(repo, "--project", "demo", "--out-dir", str(out))
    assert r.returncode == 0, r.stderr
    assert (out / "demo-state-graph.db").exists()


def test_running_twice_does_not_crash(tmp_path):
    repo = _make_repo(tmp_path)
    db = tmp_path / "demo-state-graph.db"
    r1 = _run_cli(repo, "--project", "demo", "--db-path", str(db))
    r2 = _run_cli(repo, "--project", "demo", "--db-path", str(db))
    assert r1.returncode == 0 and r2.returncode == 0
    conn = sqlite3.connect(str(db))
    runs = conn.execute("SELECT COUNT(*) FROM analysis_run").fetchone()[0]
    conn.close()
    assert runs == 2


def test_rebuild_is_idempotent_no_duplication(tmp_path):
    # PSG-C1: re-running over the same DB must NOT double the graph.
    repo = _make_repo(tmp_path)
    db = tmp_path / "demo-state-graph.db"
    _run_cli(repo, "--project", "demo", "--db-path", str(db))
    conn = sqlite3.connect(str(db))
    n1 = conn.execute("SELECT COUNT(*) FROM node").fetchone()[0]
    e1 = conn.execute("SELECT COUNT(*) FROM edge").fetchone()[0]
    conn.close()
    assert n1 > 0
    _run_cli(repo, "--project", "demo", "--db-path", str(db))
    conn = sqlite3.connect(str(db))
    n2 = conn.execute("SELECT COUNT(*) FROM node").fetchone()[0]
    e2 = conn.execute("SELECT COUNT(*) FROM edge").fetchone()[0]
    conn.close()
    assert n2 == n1, f"nodes duplicated on rebuild: {n1} -> {n2}"
    assert e2 == e1, f"edges duplicated on rebuild: {e1} -> {e2}"


def test_graph_rows_stamped_with_latest_run_id(tmp_path):
    # PSG-D2: every node/edge carries the run_id of the rebuild that produced it.
    repo = _make_repo(tmp_path)
    db = tmp_path / "demo-state-graph.db"
    _run_cli(repo, "--project", "demo", "--db-path", str(db))
    _run_cli(repo, "--project", "demo", "--db-path", str(db))
    conn = sqlite3.connect(str(db))
    latest = conn.execute("SELECT MAX(id) FROM analysis_run").fetchone()[0]
    null_nodes = conn.execute("SELECT COUNT(*) FROM node WHERE run_id IS NULL").fetchone()[0]
    distinct = conn.execute("SELECT DISTINCT run_id FROM node").fetchall()
    conn.close()
    assert null_nodes == 0
    assert distinct == [(latest,)], f"all nodes should belong to the latest run: {distinct}"


def test_qualified_name_index_exists(tmp_path):
    # PSG-D3: the most-queried columns are indexed.
    repo = _make_repo(tmp_path)
    db = tmp_path / "demo-state-graph.db"
    _run_cli(repo, "--project", "demo", "--db-path", str(db))
    conn = sqlite3.connect(str(db))
    idx = {r[1] for r in conn.execute("PRAGMA index_list(node)")}
    conn.close()
    assert {"idx_node_qualified_name", "idx_node_name"} <= idx


def test_routes_extracted(tmp_path):
    repo = _make_repo(tmp_path)
    db = tmp_path / "demo-state-graph.db"
    _run_cli(repo, "--project", "demo", "--db-path", str(db))
    conn = sqlite3.connect(str(db))
    routes = {
        r[0]
        for r in conn.execute(
            """SELECT n.name FROM node n JOIN node_type t ON n.node_type_id=t.id
               WHERE t.name='route'"""
        ).fetchall()
    }
    conn.close()
    assert "/" in routes
    assert "/refresh" in routes


def _count(db, sql):
    conn = sqlite3.connect(str(db))
    try:
        return conn.execute(sql).fetchone()[0]
    finally:
        conn.close()


def test_data_vars_and_edges_present(tmp_path):
    repo = _make_repo(tmp_path)
    db = tmp_path / "demo-state-graph.db"
    _run_cli(repo, "--project", "demo", "--db-path", str(db))
    assert _count(db, "SELECT COUNT(*) FROM node n JOIN node_type t ON n.node_type_id=t.id WHERE t.name='data_var'") > 0
    assert _count(db, "SELECT COUNT(*) FROM edge e JOIN edge_type t ON e.edge_type_id=t.id WHERE t.name='produces'") > 0


def test_cards_tables_populated_by_default(tmp_path):
    repo = _make_repo(tmp_path)
    db = tmp_path / "demo-state-graph.db"
    _run_cli(repo, "--project", "demo", "--db-path", str(db))
    assert _count(db, "SELECT COUNT(*) FROM consistency_card") > 0
    assert _count(db, "SELECT COUNT(*) FROM symbol_card") > 0


def test_no_cards_flag_skips_cards(tmp_path):
    repo = _make_repo(tmp_path)
    db = tmp_path / "demo-state-graph.db"
    r = _run_cli(repo, "--project", "demo", "--db-path", str(db), "--no-cards")
    assert r.returncode == 0, r.stderr
    # cards tables should not exist when skipped
    conn = sqlite3.connect(str(db))
    tables = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    conn.close()
    assert "consistency_card" not in tables
    assert "symbol_card" not in tables
