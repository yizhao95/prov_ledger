"""Tests for review_diff.py — git-diff vs deep-graph stale-reference review.

review_diff resolves a diff range (auto remote/local), parses renamed/removed
symbols from the git diff, and queries the project-state-graph deep sqlite graph
for callers that still reference a removed/renamed symbol.
"""
from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path

import pytest

import review_diff


# ── git fixtures ────────────────────────────────────────────────────────────────

def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True,
                          text=True, check=True).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q")
    _git(r, "config", "user.email", "t@t.com")
    _git(r, "config", "user.name", "t")
    (r / "pipeline.py").write_text(
        "def run():\n    return old_name()\n\ndef old_name():\n    return 1\n"
    )
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "base")
    return r


# ── resolve_range ───────────────────────────────────────────────────────────────

def test_resolve_range_local_when_no_upstream(repo):
    base_sha = _git(repo, "rev-parse", "HEAD")
    # make a new commit so HEAD != base
    (repo / "pipeline.py").write_text(
        "def run():\n    return old_name()\n\ndef renamed():\n    return 1\n"
    )
    _git(repo, "commit", "-aqm", "rename")
    base, head, mode = review_diff.resolve_range(str(repo), base_sha)
    assert mode == "local"
    assert head == "HEAD"
    assert base == base_sha


def test_resolve_range_remote_when_upstream(tmp_path):
    # bare 'remote' + clone with upstream tracking
    bare = tmp_path / "bare.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    work = tmp_path / "work"
    subprocess.run(["git", "clone", "-q", str(bare), str(work)], check=True)
    _git(work, "config", "user.email", "t@t.com")
    _git(work, "config", "user.name", "t")
    (work / "f.py").write_text("def a():\n    return 1\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", "c1")
    _git(work, "push", "-q", "origin", "HEAD:main")
    _git(work, "branch", "--set-upstream-to=origin/main")
    base, head, mode = review_diff.resolve_range(str(work), "deadbeef")
    assert mode == "remote"
    assert head == "HEAD"
    assert "@{u}" in base or "origin" in base


# ── changed_symbols ─────────────────────────────────────────────────────────────

def test_changed_symbols_detects_removed_def(repo):
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "pipeline.py").write_text(
        "def run():\n    return old_name()\n\ndef renamed():\n    return 1\n"
    )
    _git(repo, "commit", "-aqm", "rename old_name->renamed")
    syms = review_diff.changed_symbols(str(repo), base, "HEAD")
    removed = {s["old_name"] for s in syms if s["kind"] in ("removed", "renamed")}
    assert "old_name" in removed


def test_changed_symbols_handles_deleted_file(repo):
    # PSG-C7: a fully deleted file's removed defs attach to that file (+++ /dev/null).
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "extra.py").write_text("def gone():\n    return 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "add extra")
    base2 = _git(repo, "rev-parse", "HEAD")
    (repo / "extra.py").unlink()
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "delete extra")
    syms = review_diff.changed_symbols(str(repo), base2, "HEAD")
    removed = [s for s in syms if s["old_name"] == "gone"]
    assert removed, "removed def in a deleted file must be detected"
    assert removed[0]["file"] == "extra.py"


def test_changed_symbols_ignores_indented_methods(repo):
    # PSG-C7: an indented method rename must NOT be reported as a top-level change.
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "cls.py").write_text("class C:\n    def m_old(self):\n        return 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "add cls")
    (repo / "cls.py").write_text("class C:\n    def m_new(self):\n        return 1\n")
    _git(repo, "commit", "-aqm", "rename method")
    syms = review_diff.changed_symbols(str(repo), base, "HEAD")
    names = {s["old_name"] for s in syms} | {s.get("new_name") for s in syms}
    assert "m_old" not in names, "indented method captured as top-level symbol"
    assert "C" not in names, "unchanged class reported as a spurious rename"


# ── stale_references ─────────────────────────────────────────────────────────────

def _build_graph(db_path: Path):
    """Tiny deep graph: pipeline.run --calls--> old_name (a now-removed symbol)."""
    c = sqlite3.connect(str(db_path))
    c.executescript("""
        CREATE TABLE node_type (id INTEGER PRIMARY KEY, name TEXT UNIQUE, description TEXT);
        CREATE TABLE node (id INTEGER PRIMARY KEY, node_type_id INTEGER, name TEXT,
                           qualified_name TEXT, file_path TEXT, line_start INTEGER,
                           line_end INTEGER, metadata_json TEXT);
        CREATE TABLE edge_type (id INTEGER PRIMARY KEY, name TEXT UNIQUE, description TEXT);
        CREATE TABLE edge (id INTEGER PRIMARY KEY, edge_type_id INTEGER, src_node_id INTEGER,
                           dst_node_id INTEGER, metadata_json TEXT);
    """)
    c.execute("INSERT INTO node_type (id, name) VALUES (1, 'function')")
    c.execute("INSERT INTO edge_type (id, name) VALUES (1, 'calls')")
    c.execute("INSERT INTO node (id, node_type_id, name, qualified_name, file_path) "
              "VALUES (1, 1, 'run', 'pipeline.run', 'pipeline.py')")
    c.execute("INSERT INTO node (id, node_type_id, name, qualified_name, file_path) "
              "VALUES (2, 1, 'old_name', 'pipeline.old_name', 'pipeline.py')")
    c.execute("INSERT INTO edge (id, edge_type_id, src_node_id, dst_node_id) "
              "VALUES (1, 1, 1, 2)")
    c.commit()
    c.close()


def test_stale_references_finds_hit(tmp_path):
    db = tmp_path / "graph.db"
    _build_graph(db)
    hits = review_diff.stale_references(str(db), ["old_name"])
    assert hits, "expected a stale reference to old_name"
    assert any(h["callee"] == "old_name" for h in hits)
    assert any(h["caller"] in ("run", "pipeline.run") for h in hits)


def test_stale_references_none_when_clean(tmp_path):
    db = tmp_path / "graph.db"
    _build_graph(db)
    hits = review_diff.stale_references(str(db), ["some_other_symbol"])
    assert hits == []


# ── report ──────────────────────────────────────────────────────────────────────

def test_report_not_ok_with_gaps(tmp_path):
    db = tmp_path / "graph.db"
    _build_graph(db)
    rep = review_diff.report(str(db), [{"old_name": "old_name", "kind": "removed"}])
    assert rep["ok"] is False
    assert rep["gaps"]
    assert "old_name" in rep["text"]


def test_report_ok_when_clean(tmp_path):
    db = tmp_path / "graph.db"
    _build_graph(db)
    rep = review_diff.report(str(db), [{"old_name": "nonexistent", "kind": "removed"}])
    assert rep["ok"] is True
    assert rep["gaps"] == []


# ── data drift gates (v2) ────────────────────────────────────────────────────────

def _build_data_graph(db_path: Path):
    """Graph with a dataframe->column, a produces/consumes dtype chain, and a
    lineage edge, so the data-drift gates have something to check."""
    c = sqlite3.connect(str(db_path))
    c.executescript("""
        CREATE TABLE node_type (id INTEGER PRIMARY KEY, name TEXT UNIQUE, description TEXT);
        CREATE TABLE node (id INTEGER PRIMARY KEY, node_type_id INTEGER, name TEXT,
                           qualified_name TEXT, file_path TEXT, line_start INTEGER,
                           line_end INTEGER, metadata_json TEXT);
        CREATE TABLE edge_type (id INTEGER PRIMARY KEY, name TEXT UNIQUE, description TEXT);
        CREATE TABLE edge (id INTEGER PRIMARY KEY, edge_type_id INTEGER, src_node_id INTEGER,
                           dst_node_id INTEGER, metadata_json TEXT);
    """)
    c.execute("INSERT INTO node_type (id, name) VALUES (1,'function'),(2,'column'),(3,'data_var')")
    c.execute("INSERT INTO edge_type (id, name) VALUES (1,'has_column'),(2,'produces'),(3,'consumes'),(4,'lineage')")
    # dataframe-ish owner (function) -> column 'amount'
    c.execute("INSERT INTO node (id, node_type_id, name, qualified_name, file_path, metadata_json) "
              "VALUES (1,1,'load','load','etl.py',NULL)")
    c.execute("INSERT INTO node (id, node_type_id, name, qualified_name, file_path, metadata_json) "
              "VALUES (2,2,'amount','amount','etl.py','{\"dtype\": \"int64\"}')")
    c.execute("INSERT INTO edge (id, edge_type_id, src_node_id, dst_node_id) VALUES (1,1,1,2)")
    c.commit()
    c.close()


def test_removed_column_is_a_gap(tmp_path):
    db = tmp_path / "g.db"
    _build_data_graph(db)
    rep = review_diff.data_drift(str(db), removed_columns=["amount"],
                                 dtype_changes=[], removed_datasets=[])
    assert rep["ok"] is False
    assert any("amount" in g.get("detail", "") for g in rep["gaps"])


def test_dtype_change_is_a_gap(tmp_path):
    db = tmp_path / "g.db"
    _build_data_graph(db)
    # graph has amount=int64; a change to float64 should be flagged
    rep = review_diff.data_drift(str(db), removed_columns=[],
                                 dtype_changes=[{"column": "amount", "new_dtype": "float64"}],
                                 removed_datasets=[])
    assert rep["ok"] is False
    assert any("amount" in g.get("detail", "") for g in rep["gaps"])


def test_clean_data_drift_ok(tmp_path):
    db = tmp_path / "g.db"
    _build_data_graph(db)
    rep = review_diff.data_drift(str(db), removed_columns=[],
                                 dtype_changes=[], removed_datasets=[])
    assert rep["ok"] is True
    assert rep["gaps"] == []


def test_data_drift_reports_dont_autofix(tmp_path):
    db = tmp_path / "g.db"
    _build_data_graph(db)
    rep = review_diff.data_drift(str(db), removed_columns=["amount"],
                                 dtype_changes=[], removed_datasets=[])
    # report-only: the gap is described, nothing is mutated in the graph
    assert "text" in rep and "amount" in rep["text"]
    c = sqlite3.connect(str(db))
    still = c.execute("SELECT COUNT(*) FROM node WHERE name='amount'").fetchone()[0]
    c.close()
    assert still == 1  # untouched


# ── A-5 combined verdict (ANDs all active gates) ─────────────────────────────────

def _git_a5(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, capture_output=True,
                          text=True, check=True).stdout.strip()


def _repo_with_signature_break(tmp_path):
    """A repo whose HEAD changes target()'s return annotation, leaving caller.py
    untouched, plus a graph whose consistency_card wires caller->target."""
    import json
    r = tmp_path / "repo"
    r.mkdir()
    _git_a5(r, "init", "-q")
    _git_a5(r, "config", "user.email", "t@t.com")
    _git_a5(r, "config", "user.name", "t")
    (r / "mod.py").write_text("def target(a) -> int:\n    return a\n")
    (r / "caller.py").write_text("from mod import target\n\ndef caller():\n    return target(1)\n")
    _git_a5(r, "add", "-A"); _git_a5(r, "commit", "-qm", "base")
    base = _git_a5(r, "rev-parse", "HEAD")
    (r / "mod.py").write_text("def target(a) -> dict:\n    return {}\n")
    _git_a5(r, "add", "-A"); _git_a5(r, "commit", "-qm", "change return")
    head = _git_a5(r, "rev-parse", "HEAD")

    db = tmp_path / "g.db"
    c = sqlite3.connect(str(db))
    c.executescript("""
        CREATE TABLE node_type (id INTEGER PRIMARY KEY, name TEXT UNIQUE, description TEXT);
        CREATE TABLE node (id INTEGER PRIMARY KEY, node_type_id INTEGER, name TEXT,
                           qualified_name TEXT, file_path TEXT, line_start INTEGER,
                           line_end INTEGER, metadata_json TEXT);
        CREATE TABLE edge_type (id INTEGER PRIMARY KEY, name TEXT UNIQUE, description TEXT);
        CREATE TABLE edge (id INTEGER PRIMARY KEY, edge_type_id INTEGER, src_node_id INTEGER,
                           dst_node_id INTEGER, metadata_json TEXT);
        CREATE TABLE consistency_card (symbol_id INTEGER PRIMARY KEY, card_json TEXT NOT NULL);
    """)
    c.execute("INSERT INTO node_type (id, name) VALUES (1,'function')")
    c.execute("INSERT INTO node (id, node_type_id, name, qualified_name, file_path) "
              "VALUES (1,1,'target','target','mod.py'),(2,1,'caller','caller','caller.py')")
    card = {"callers": ["caller"], "callees": [], "output_consumers": ["caller"],
            "reads": [], "writes": [], "pipeline_membership": [], "dtype_map": {},
            "columns_in": [], "columns_out": [], "lineage_upstream": [],
            "lineage_downstream": [], "profile": []}
    c.execute("INSERT INTO consistency_card (symbol_id, card_json) VALUES (1, ?)",
              (json.dumps(card),))
    c.commit(); c.close()
    return str(repo := r), str(db), base, head


def test_full_verdict_fails_on_signature_gap(tmp_path):
    repo, db, base, head = _repo_with_signature_break(tmp_path)
    rep = review_diff.full_verdict(db, repo, base, head, changed=[])
    assert rep["ok"] is False
    # the assembled text names the signature gate
    assert "signature" in rep["text"].lower()
    assert "signature" in rep["gates"]


def test_full_verdict_ands_all_gates(tmp_path):
    # clean repo: a body-only change, no graph breaks -> all gates True -> ok
    r = tmp_path / "repo"
    r.mkdir()
    _git_a5(r, "init", "-q")
    _git_a5(r, "config", "user.email", "t@t.com")
    _git_a5(r, "config", "user.name", "t")
    (r / "mod.py").write_text("def f(a) -> int:\n    return a\n")
    _git_a5(r, "add", "-A"); _git_a5(r, "commit", "-qm", "base")
    base = _git_a5(r, "rev-parse", "HEAD")
    (r / "mod.py").write_text("def f(a) -> int:\n    x = a\n    return x\n")
    _git_a5(r, "add", "-A"); _git_a5(r, "commit", "-qm", "body only")
    head = _git_a5(r, "rev-parse", "HEAD")

    db = tmp_path / "g.db"
    c = sqlite3.connect(str(db))
    c.executescript("""
        CREATE TABLE node_type (id INTEGER PRIMARY KEY, name TEXT UNIQUE, description TEXT);
        CREATE TABLE node (id INTEGER PRIMARY KEY, node_type_id INTEGER, name TEXT,
                           qualified_name TEXT, file_path TEXT, line_start INTEGER,
                           line_end INTEGER, metadata_json TEXT);
        CREATE TABLE edge_type (id INTEGER PRIMARY KEY, name TEXT UNIQUE, description TEXT);
        CREATE TABLE edge (id INTEGER PRIMARY KEY, edge_type_id INTEGER, src_node_id INTEGER,
                           dst_node_id INTEGER, metadata_json TEXT);
        CREATE TABLE consistency_card (symbol_id INTEGER PRIMARY KEY, card_json TEXT NOT NULL);
    """)
    c.commit(); c.close()
    rep = review_diff.full_verdict(str(db), str(r), base, head, changed=[])
    assert rep["ok"] is True
    # the result reports per-gate booleans
    assert set(rep["gates"]) >= {"stale_references", "data_drift", "signature", "sql_contract"}
    assert all(rep["gates"].values())
