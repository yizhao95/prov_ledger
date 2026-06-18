"""Tests for impact_preflight — provLedger Phase D plan-time forward impact analysis.

impact_preflight is stdlib-only (sqlite3+json+re) and imports nothing from the
orchestrator package, so it stays a pure, fast unit under test.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import impact_preflight  # noqa: E402


# ── shared graph fixture helpers ─────────────────────────────────────────────────

def _new_db(path):
    c = sqlite3.connect(str(path))
    c.executescript(
        """
        CREATE TABLE node_type (id INTEGER PRIMARY KEY, name TEXT UNIQUE);
        CREATE TABLE node (id INTEGER PRIMARY KEY, node_type_id INTEGER, name TEXT,
                           qualified_name TEXT, file_path TEXT, metadata_json TEXT);
        CREATE TABLE edge_type (id INTEGER PRIMARY KEY, name TEXT UNIQUE);
        CREATE TABLE edge (id INTEGER PRIMARY KEY, edge_type_id INTEGER,
                           src_node_id INTEGER, dst_node_id INTEGER, metadata_json TEXT);
        """
    )
    return c


def _nt(c, name):
    c.execute("INSERT OR IGNORE INTO node_type (name) VALUES (?)", (name,))
    return c.execute("SELECT id FROM node_type WHERE name=?", (name,)).fetchone()[0]


def _et(c, name):
    c.execute("INSERT OR IGNORE INTO edge_type (name) VALUES (?)", (name,))
    return c.execute("SELECT id FROM edge_type WHERE name=?", (name,)).fetchone()[0]


def _node(c, ntype, name, *, qname=None, fp=None, meta=None):
    tid = _nt(c, ntype)
    cur = c.execute(
        "INSERT INTO node (node_type_id,name,qualified_name,file_path,metadata_json) "
        "VALUES (?,?,?,?,?)",
        (tid, name, qname or name, fp, json.dumps(meta) if meta is not None else None))
    return cur.lastrowid


def _edge(c, etype, s, d, *, meta=None):
    tid = _et(c, etype)
    c.execute("INSERT INTO edge (edge_type_id,src_node_id,dst_node_id,metadata_json) "
              "VALUES (?,?,?,?)", (tid, s, d, json.dumps(meta) if meta is not None else None))


@pytest.fixture
def graph_db(tmp_path):
    """A->B calls; B produces dv (dtype int) consumed by C; sql_table sales.daily."""
    db = tmp_path / "g.db"
    c = _new_db(db)
    a = _node(c, "function", "alpha", qname="mod.alpha", fp="mod.py")
    b = _node(c, "function", "process", qname="pipeline.process", fp="pipeline.py")
    cc = _node(c, "function", "consumer", qname="mod.consumer", fp="mod.py")
    dv = _node(c, "data_var", "process:return", fp="pipeline.py",
               meta={"dtype": "int"})
    dv2 = _node(c, "data_var", "downstream", fp="mod.py", meta={"dtype": "int"})
    _edge(c, "calls", a, b)            # alpha -> process (alpha is a caller of process)
    _edge(c, "produces", b, dv, meta={"type": "int"})
    _edge(c, "consumes", dv, cc, meta={"type": "int"})
    _edge(c, "feeds", dv, dv2, meta={"dtype": "int"})
    _edge(c, "downstream_data_feed", b, dv2)
    # SQL source
    loader = _node(c, "function", "load_sales", qname="loader.load_sales", fp="loader.py")
    tbl = _node(c, "sql_table", "sales.daily",
                meta={"assumed_schema": {"store_id": "unknown", "amount": "unknown"}})
    _edge(c, "reads_sql", loader, tbl)
    c.commit()
    c.close()
    return str(db)


# ── collect_targets ──────────────────────────────────────────────────────────────

def test_collect_declared_targets_included(graph_db):
    out = impact_preflight.collect_targets("", ["pipeline.process"], graph_db)
    names = {t["name"] for t in out["targets"]}
    assert "pipeline.process" in names
    decl = next(t for t in out["targets"] if t["name"] == "pipeline.process")
    assert any("declared" in r for r in decl["route"])


def test_collect_keyword_reverse_lookup(graph_db):
    out = impact_preflight.collect_targets("please refactor the process function", [], graph_db)
    names = {t["name"] for t in out["targets"]}
    assert "process" in names or "pipeline.process" in names
    hit = next(t for t in out["targets"] if t["name"] in ("process", "pipeline.process"))
    assert any("keyword" in r for r in hit["route"])


def test_collect_union_dedupes_both_routes(graph_db):
    out = impact_preflight.collect_targets("touch the process step", ["process"], graph_db)
    procs = [t for t in out["targets"] if t["name"] == "process"]
    assert len(procs) == 1                       # union, deduped
    assert {"declared", "keyword"} <= {r for r in procs[0]["route"]} \
        or len(procs[0]["route"]) >= 2


def test_collect_ignores_short_and_stopword_tokens(graph_db):
    out = impact_preflight.collect_targets("to do it as is", [], graph_db)
    assert out["targets"] == []                  # nothing real matched


def test_collect_empty(graph_db):
    out = impact_preflight.collect_targets("", [], graph_db)
    assert out["targets"] == []


# ── verify_symbol ────────────────────────────────────────────────────────────────

def test_verify_existing_symbol(graph_db):
    conn = sqlite3.connect(graph_db); conn.row_factory = sqlite3.Row
    try:
        out = impact_preflight.verify_symbol(conn, "pipeline.process")
    finally:
        conn.close()
    assert out["status"] == "existing"
    assert "mod.alpha" in out["callers"]                 # incoming calls
    assert "mod.consumer" in out["output_consumers"]     # produces -> consumes
    assert out["dtype_map"]                               # has dtype info
    assert any("int" in str(v) for v in out["dtype_map"].values())
    assert "downstream" in [str(x) for x in out["lineage_downstream"]] \
        or out["lineage_downstream"]                      # 1-2 hop lineage


def test_verify_missing_symbol_is_new(graph_db):
    conn = sqlite3.connect(graph_db); conn.row_factory = sqlite3.Row
    try:
        out = impact_preflight.verify_symbol(conn, "compute_rolling_window")
    finally:
        conn.close()
    assert out["status"] == "new"
    assert out["callers"] == []
    assert out["output_consumers"] == []
    assert out["lineage_downstream"] == []


def test_verify_deterministic_sorted(graph_db):
    conn = sqlite3.connect(graph_db); conn.row_factory = sqlite3.Row
    try:
        out = impact_preflight.verify_symbol(conn, "pipeline.process")
    finally:
        conn.close()
    assert out["callers"] == sorted(out["callers"])
    assert out["output_consumers"] == sorted(out["output_consumers"])


# ── upstream_assumptions ─────────────────────────────────────────────────────────

def test_upstream_assumptions_surfaced(graph_db):
    conn = sqlite3.connect(graph_db); conn.row_factory = sqlite3.Row
    try:
        out = impact_preflight.upstream_assumptions(conn, ["loader.load_sales"])
    finally:
        conn.close()
    assert len(out) == 1
    a = out[0]
    assert a["table"] == "sales.daily"
    assert set(a["columns"]) == {"store_id", "amount"}
    assert "assert" in a["recommendation"].lower()
    assert "load" in a["recommendation"].lower()


def test_upstream_assumptions_none_when_no_sql(tmp_path):
    db = tmp_path / "nosql.db"
    c = _new_db(db)
    _node(c, "function", "plain", qname="m.plain", fp="m.py")
    c.commit(); c.close()
    conn = sqlite3.connect(str(db)); conn.row_factory = sqlite3.Row
    try:
        out = impact_preflight.upstream_assumptions(conn, ["m.plain"])
    finally:
        conn.close()
    assert out == []


# ── ledger_matches (Phase E stub) + compute_impact_context ───────────────────────

def test_ledger_matches_no_db_path_empty(tmp_path):
    # a fresh DB with no LedgerEntries table -> [] (no crash)
    import sqlite3 as _s
    p = tmp_path / "empty.db"
    _s.connect(str(p)).close()
    assert impact_preflight.ledger_matches(str(p), "proj", "anything", ["x"]) == []


def test_compute_impact_context_shape(graph_db):
    ic = impact_preflight.compute_impact_context(
        graph_db, "refactor the process function", ["pipeline.process"])
    assert set(ic) >= {"targets", "symbols", "upstream_assumptions",
                       "ledger_reminders", "capability_boundary", "generated_at"}
    # existing symbol resolved with its real caller
    procsym = next(s for s in ic["symbols"] if s["name"] == "pipeline.process")
    assert procsym["status"] == "existing"
    assert "mod.alpha" in procsym["callers"]
    # JSON round-trip
    assert json.loads(json.dumps(ic)) == ic


def test_compute_impact_context_new_code_boundary(graph_db):
    ic = impact_preflight.compute_impact_context(
        graph_db, "", ["compute_rolling_window"])
    sym = next(s for s in ic["symbols"] if s["name"] == "compute_rolling_window")
    assert sym["status"] == "new"
    assert "new" in ic["capability_boundary"].lower()


# ── Phase E: real ledger_matches (deterministic fuzzy match) ─────────────────────

import ledger_store  # noqa: E402
ORCH = Path.home() / "skill-workspace" / "orchestrator"
sys.path.insert(0, str(ORCH))
from orchestrator import db as _orch_db  # noqa: E402


@pytest.fixture
def ledger_db(tmp_path):
    p = tmp_path / "ledger.db"
    c = sqlite3.connect(str(p)); c.row_factory = sqlite3.Row
    _orch_db.run_migrations(c)
    ledger_store.add_entry(
        c, project="proj", kind="decision",
        statement="rolling-window split, not random split",
        rationale="random split leaks temporal information",
        subjects=["train_test_split", "split"],
        keywords=["split", "rolling", "temporal"], source="manual")
    ledger_store.add_entry(
        c, project="proj", kind="anti_pattern",
        statement="changing UPC from BIGINT to STRING",
        rationale="downstream joins broke on dtype mismatch",
        subjects=["upc"], keywords=["upc", "bigint", "string", "dtype"],
        source="manual")
    c.commit(); c.close()
    return str(p)


def test_ledger_matches_decision_surfaces(ledger_db):
    out = impact_preflight.ledger_matches(
        ledger_db, "proj", "change the train/test split logic", ["train_test_split"])
    assert out, "expected the rolling-window decision to match"
    top = out[0]
    assert top["kind"] == "decision"
    assert top["score"] > 0
    assert top["reminder"].lower().startswith("reminder:")
    assert "temporal" in top["reminder"].lower()


def test_ledger_matches_anti_pattern_warns(ledger_db):
    out = impact_preflight.ledger_matches(
        ledger_db, "proj", "change UPC from BIGINT to STRING", ["upc"])
    kinds = {m["kind"] for m in out}
    assert "anti_pattern" in kinds
    ap = next(m for m in out if m["kind"] == "anti_pattern")
    assert "tried and failed" in ap["reminder"].lower() or "warning" in ap["reminder"].lower()


def test_ledger_matches_unrelated_empty(ledger_db):
    out = impact_preflight.ledger_matches(
        ledger_db, "proj", "update the readme documentation footer", [])
    assert out == []


def test_ledger_matches_superseded_excluded(ledger_db):
    c = sqlite3.connect(ledger_db)
    ids = [r[0] for r in c.execute("SELECT id FROM LedgerEntries").fetchall()]
    for i in ids:
        c.execute("UPDATE LedgerEntries SET status='superseded' WHERE id=?", (i,))
    c.commit(); c.close()
    out = impact_preflight.ledger_matches(
        ledger_db, "proj", "change the train/test split", ["train_test_split"])
    assert out == []


def test_ledger_matches_top_n_cap(ledger_db):
    out = impact_preflight.ledger_matches(
        ledger_db, "proj", "split upc dtype string rolling temporal bigint",
        ["train_test_split", "upc"], top_n=1)
    assert len(out) == 1


def test_ledger_matches_no_table_degrades(graph_db):
    # graph_db has no LedgerEntries table -> must return [] not raise
    out = impact_preflight.ledger_matches(graph_db, "proj", "anything", ["x"])
    assert out == []


# ── Phase E: compute_impact_context populates ledger_reminders ───────────────────

def test_compute_impact_context_populates_ledger(tmp_path):
    # graph + ledger share one DB file (orchestrator-style)
    db = tmp_path / "combo.db"
    c = sqlite3.connect(str(db)); c.row_factory = sqlite3.Row
    _orch_db.run_migrations(c)
    # minimal graph node so collect_targets/verify_symbol work
    c.executescript(
        """
        CREATE TABLE node_type (id INTEGER PRIMARY KEY, name TEXT UNIQUE);
        CREATE TABLE node (id INTEGER PRIMARY KEY, node_type_id INTEGER, name TEXT,
                           qualified_name TEXT, file_path TEXT, metadata_json TEXT);
        CREATE TABLE edge_type (id INTEGER PRIMARY KEY, name TEXT UNIQUE);
        CREATE TABLE edge (id INTEGER PRIMARY KEY, edge_type_id INTEGER,
                           src_node_id INTEGER, dst_node_id INTEGER, metadata_json TEXT);
        INSERT INTO node_type (name) VALUES ('function');
        INSERT INTO node (node_type_id,name,qualified_name,file_path) VALUES
          (1,'train_test_split','pipeline.train_test_split','pipeline.py');
        """)
    ledger_store.add_entry(
        c, project="proj", kind="decision",
        statement="rolling-window split, not random split",
        rationale="random split leaks temporal information",
        subjects=["train_test_split", "split"],
        keywords=["split", "rolling", "temporal"], source="manual")
    c.commit(); c.close()

    ic = impact_preflight.compute_impact_context(
        str(db), "change the train/test split logic",
        ["train_test_split"], project="proj")
    assert ic["ledger_reminders"], "expected ledger reminder to surface"
    assert any("temporal" in r["reminder"].lower() for r in ic["ledger_reminders"])
    # still JSON-serializable
    assert json.loads(json.dumps(ic)) == ic


def test_compute_impact_context_empty_ledger_ok(graph_db):
    # graph_db has no LedgerEntries table -> ledger_reminders == [] (no crash)
    ic = impact_preflight.compute_impact_context(
        graph_db, "refactor process", ["pipeline.process"], project="proj")
    assert ic["ledger_reminders"] == []
