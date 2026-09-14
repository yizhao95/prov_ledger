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


# ── FL-020: a busy graph (review refresh holds the write lock) must degrade ──

def test_fl020_busy_graph_degrades_instead_of_crashing(graph_db, monkeypatch):
    monkeypatch.setattr(impact_preflight, "BUSY_TIMEOUT_MS", 100)
    holder = sqlite3.connect(graph_db)
    holder.execute("BEGIN EXCLUSIVE")           # what init_project.sh's rebuild looks like from outside
    try:
        ctx = impact_preflight.compute_impact_context(graph_db, "tweak process", ["pipeline.process"], project="proj")
    finally:
        holder.rollback(); holder.close()
    assert ctx["degraded"] == "graph busy"
    assert ctx["symbols"] == [] and ctx["upstream_assumptions"] == [] and ctx["ledger_reminders"] == []
    assert [t["name"] for t in ctx["targets"]] == ["pipeline.process"]     # declared targets kept verbatim
    assert "graph busy" in ctx["capability_boundary"]
    # once the lock is gone the same call is a normal, non-degraded context
    ctx2 = impact_preflight.compute_impact_context(graph_db, "tweak process", ["pipeline.process"], project="proj")
    assert "degraded" not in ctx2 and ctx2["symbols"][0]["status"] == "existing"


def test_connect_is_read_only(graph_db):
    conn = impact_preflight._connect(graph_db)
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("INSERT INTO node_type (name) VALUES ('x')")
    conn.close()


# ── 3.4-A: one-lookup impact — history + reasons + constraints (E2-1 … E2-3, E4-1/E4-2) ──

STORE_PY = Path(__file__).resolve().parents[2] / "project-state-graph" / "scripts" / "analyzer" / "store.py"
# VERBATIM copy of analyzer/store.py::_HISTORY_SCHEMA (test_history_schema_matches_analyzer guards drift)
HISTORY_SCHEMA = """
CREATE TABLE IF NOT EXISTS node_snapshot (
    id               INTEGER PRIMARY KEY,
    run_id           INTEGER NOT NULL REFERENCES analysis_run(id),
    node_key         TEXT NOT NULL,
    node_type        TEXT NOT NULL,
    qualified_name   TEXT NOT NULL,
    file_path        TEXT,
    line_start       INTEGER,
    line_end         INTEGER,
    struct_sig       TEXT,
    dataflow_sig     TEXT,
    dataflow_trivial INTEGER NOT NULL DEFAULT 1,
    attrs_json       TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS node_event (
    id           INTEGER PRIMARY KEY,
    run_id       INTEGER NOT NULL REFERENCES analysis_run(id),
    seq          INTEGER NOT NULL,
    event_type   TEXT NOT NULL,
    node_key     TEXT,
    tier         TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at   TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_node_snapshot_run_key ON node_snapshot(run_id, node_key)
    WHERE node_key <> '';
CREATE INDEX IF NOT EXISTS idx_node_snapshot_run_qn ON node_snapshot(run_id, node_type, qualified_name);
CREATE INDEX IF NOT EXISTS idx_node_event_key ON node_event(node_key, run_id);
CREATE TRIGGER IF NOT EXISTS trg_node_event_no_update BEFORE UPDATE ON node_event
    BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_node_event_no_delete BEFORE DELETE ON node_event
    BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_node_snapshot_no_delete BEFORE DELETE ON node_snapshot
    BEGIN SELECT RAISE(ABORT, 'append-only'); END;
"""


def test_history_schema_matches_analyzer():
    assert HISTORY_SCHEMA in STORE_PY.read_text()


@pytest.fixture
def graph_db_with_history(graph_db):
    """graph_db + analysis_run / node_snapshot / node_event + node.node_key:
    pipeline.process is nk_a and was node_changed by plan P0; a column of it is nk_c."""
    c = sqlite3.connect(graph_db)
    c.executescript("""
        ALTER TABLE node ADD COLUMN node_key TEXT;
        CREATE TABLE analysis_run (id INTEGER PRIMARY KEY, project_name TEXT NOT NULL, commit_sha TEXT,
            started_at TEXT NOT NULL, finished_at TEXT, tool_version TEXT, plan_id TEXT, step_id TEXT, trigger TEXT);
        INSERT INTO analysis_run VALUES (1, 'proj', 'c0ffee', '2026-09-11T00:00:00+00:00', NULL, '0.1', 'P0', 'P0-REVIEW.1', 'review');
        INSERT INTO analysis_run VALUES (2, 'proj', 'd0d0', '2026-09-11T01:00:00+00:00', NULL, '0.1', 'P0b', 'P0b-A', 'update');
    """ + HISTORY_SCHEMA + """
        UPDATE node SET node_key='nk_a' WHERE qualified_name='pipeline.process';
        INSERT INTO node_snapshot (run_id, node_key, node_type, qualified_name, file_path, struct_sig, dataflow_sig, dataflow_trivial, attrs_json)
            VALUES (1, 'nk_a', 'function', 'pipeline.process', 'pipeline.py', 's1', NULL, 1, '{}'),
                   (1, 'nk_c', 'column', 'pipeline.process:df.amount', 'pipeline.py', NULL, NULL, 1, '{}'),
                   (2, 'nk_a', 'function', 'pipeline.process', 'pipeline.py', 's2', NULL, 1, '{}');
        INSERT INTO node_event (run_id, seq, event_type, node_key, tier, payload_json, created_at)
            VALUES (1, 1, 'node_added', 'nk_a', 'observed', '{}', '2026-09-11T00:00:01+00:00'),
                   (1, 2, 'node_added', 'nk_c', 'observed', '{}', '2026-09-11T00:00:01+00:00'),
                   (2, 1, 'node_matched', 'nk_a', 'observed', '{"via": "qualname"}', '2026-09-11T01:00:01+00:00'),
                   (2, 2, 'node_changed', 'nk_a', 'observed', '{"changed": ["struct_sig"]}', '2026-09-11T01:00:01+00:00');
    """)
    c.commit(); c.close()
    return graph_db


def _orch(tmp_path, visibility="shared"):
    from orchestrator import db as orch_db
    import ledger_store
    c = orch_db.open_db(tmp_path / "orch.db")
    orch_db.run_migrations(c)
    orch_db.insert_node_reason(c, node_key="nk_a", project="proj", run_id=2, plan_id="P0b", kind="reason",
                               text="fiscal weeks", source="agent", tier="stated")
    ledger_store.add_entry(c, project="proj", kind="constraint", statement="exclude region X from the rollup",
                           rationale="legal hold on region X", subjects=["nk_a"], keywords=["zzz"],
                           why_ref="https://wiki/decisions/42", why_visibility=visibility)
    return c


@pytest.fixture
def orch_db(tmp_path):
    return _orch(tmp_path)


@pytest.fixture
def orch_db_restricted(tmp_path):
    return _orch(tmp_path, visibility="restricted")


def test_e2_1_impact_context_joins_history_reasons_constraints(graph_db_with_history, orch_db):
    ctx = impact_preflight.compute_impact_context(graph_db_with_history, "tweak process", ["pipeline.process"],
                                                  project="proj", orch_conn=orch_db)
    sym = next(s for s in ctx["symbols"] if s["name"] == "pipeline.process")
    assert sym["status"] == "existing" and sym["node_key"] == "nk_a"
    assert [h["event_type"] for h in sym["history"]] == ["node_added", "node_matched", "node_changed"]
    assert sym["history"][-1]["plan_id"] == "P0b" and sym["history"][-1]["step_id"] == "P0b-A"
    assert sym["history"][-1]["changed"] == ["struct_sig"]          # slim entry (phase 3.5): no payload blob
    assert sym["reasons"][0]["text"] == "fiscal weeks" and sym["reasons"][0]["plan_id"] == "P0b"
    c = sym["constraints"][0]
    assert c["statement"].startswith("exclude region") and c["rationale"].startswith("legal hold")
    assert ctx["constraint_ids"] == [c["id"]]
    assert isinstance(ctx["approx_tokens"], int) and ctx["approx_tokens"] > 0


def test_e2_2_constraint_hit_by_node_key_not_lexical(graph_db_with_history, orch_db):
    # zero token overlap between the constraint (statement/keywords) and query/targets: still surfaced, hit recorded
    ctx = impact_preflight.compute_impact_context(graph_db_with_history, "tweak process", ["pipeline.process"],
                                                  project="proj", orch_conn=orch_db)
    assert ctx["ledger_reminders"] == []                                   # lexical route found nothing
    assert len(ctx["symbols"][0]["constraints"]) == 1                      # node_key route did
    assert orch_db.execute("SELECT hit_count FROM LedgerEntries").fetchone()[0] == 1
    impact_preflight.compute_impact_context(graph_db_with_history, "tweak process", ["pipeline.process"],
                                            project="proj", orch_conn=orch_db)
    assert orch_db.execute("SELECT hit_count FROM LedgerEntries").fetchone()[0] == 2
    # a symbol without a key gets no constraints; the plan-less call gets nothing from the ledger
    sym = next(s for s in impact_preflight.compute_impact_context(graph_db_with_history, "", ["mod.alpha"],
                                                                  project="proj", orch_conn=orch_db)["symbols"])
    assert sym["node_key"] is None and sym["constraints"] == [] and sym["reasons"] == []


def test_e4_2_restricted_why_gives_ref_only(graph_db_with_history, orch_db_restricted):
    c = impact_preflight.compute_impact_context(graph_db_with_history, "tweak process", ["pipeline.process"],
                                                project="proj", orch_conn=orch_db_restricted)["symbols"][0]["constraints"][0]
    assert c["rationale"] is None and c["why_ref"] == "https://wiki/decisions/42" and c["why_visibility"] == "restricted"


def test_e2_3_token_budget_reported_and_bounded(graph_db_with_history, orch_db):
    ctx = impact_preflight.compute_impact_context(graph_db_with_history, "tweak process",
                                                  ["pipeline.process", "mod.alpha"], project="proj", orch_conn=orch_db)
    assert ctx["approx_tokens"] == len(json.dumps({k: v for k, v in ctx.items() if k != "approx_tokens"}, default=str)) // 4
    assert ctx["approx_tokens"] <= 1500 * len(ctx["symbols"]) + 500
    assert all(len(s["history"]) <= 10 for s in ctx["symbols"])


def test_column_target_resolves_via_snapshot(graph_db_with_history):
    conn = sqlite3.connect(graph_db_with_history); conn.row_factory = sqlite3.Row
    sym = impact_preflight.verify_symbol(conn, "pipeline.process:df.amount")
    assert sym["status"] == "existing" and sym["node_key"] == "nk_c" and sym["kind"] == "column"
    assert [h["event_type"] for h in sym["history"]] == ["node_added"]
    assert sym["reasons"] == [] and sym["constraints"] == []                # no orch_conn
    conn.close()


def test_verify_symbol_without_history_tables_still_works(graph_db):
    conn = sqlite3.connect(graph_db); conn.row_factory = sqlite3.Row
    sym = impact_preflight.verify_symbol(conn, "pipeline.process")
    assert sym["status"] == "existing" and sym["node_key"] is None and sym["history"] == []
    conn.close()


# ── phase 3.5 Task 5: slimmer history entries, HISTORY_LIMIT 5 ───────────────

def test_history_entries_are_slim(graph_db_with_history):
    conn = sqlite3.connect(graph_db_with_history); conn.row_factory = sqlite3.Row
    sym = impact_preflight.verify_symbol(conn, "pipeline.process")
    for h in sym["history"]:
        assert set(h) <= {"event_type", "run_id", "plan_id", "step_id", "trigger", "created_at", "via", "changed", "from", "to"}
        assert "struct_sig" not in h and "span" not in h and "payload" not in h
    changed = next(h for h in sym["history"] if h["event_type"] == "node_changed")
    assert changed["changed"] == ["struct_sig"]
    matched = next(h for h in sym["history"] if h["event_type"] == "node_matched")
    assert matched["via"] == "qualname"
    conn.close()


def test_history_limit_defaults_to_five(graph_db_with_history):
    conn = sqlite3.connect(graph_db_with_history)
    for seq in range(3, 9):     # six more events on nk_a in run 2 -> 8 events total
        conn.execute("INSERT INTO node_event (run_id, seq, event_type, node_key, tier, payload_json, created_at) "
                     "VALUES (2, ?, 'node_changed', 'nk_a', 'observed', '{\"changed\": [\"struct_sig\"]}', ?)",
                     (seq, f"2026-09-11T01:00:{seq:02d}+00:00"))
    conn.commit(); conn.row_factory = sqlite3.Row
    assert impact_preflight.HISTORY_LIMIT == 5
    sym = impact_preflight.verify_symbol(conn, "pipeline.process")
    assert len(sym["history"]) == 5 and sym["history"][-1]["run_id"] == 2      # newest kept
    assert len(impact_preflight.verify_symbol(conn, "pipeline.process", history_limit=2)["history"]) == 2
    conn.close()


def test_e2_3_five_targets_stay_under_four_thousand_tokens(graph_db_with_history, orch_db):
    conn = sqlite3.connect(graph_db_with_history)
    for i in range(5):
        conn.execute("INSERT INTO node (node_type_id, name, qualified_name, file_path, node_key) VALUES (1, ?, ?, 'pipeline.py', ?)",
                     (f"fn{i}", f"pipeline.fn{i}", f"nk_{i}"))
        for seq in range(1, 6):
            conn.execute("INSERT INTO node_event (run_id, seq, event_type, node_key, tier, payload_json, created_at) "
                         "VALUES (2, ?, 'node_changed', ?, 'observed', '{\"changed\": [\"struct_sig\"], \"struct_sig\": {\"from\": \"aaaaaaaaaaaaaaaa\", \"to\": \"bbbbbbbbbbbbbbbb\"}, \"span\": {\"from\": [1, 9], \"to\": [1, 12]}}', ?)",
                         (100 + i * 10 + seq, f"nk_{i}", f"2026-09-11T02:00:{seq:02d}+00:00"))
        for j in range(3):
            orch_db.execute("INSERT INTO node_reason (node_key, project, run_id, plan_id, kind, text, source, tier) "
                            "VALUES (?, 'proj', 2, 'P0b', 'reason', ?, 'agent', 'stated')", (f"nk_{i}", f"reason {j} for fn{i}: kept the weekly grain"))
    conn.commit(); orch_db.commit(); conn.close()
    ctx = impact_preflight.compute_impact_context(graph_db_with_history, "tweak", [f"pipeline.fn{i}" for i in range(5)],
                                                  project="proj", orch_conn=orch_db)
    assert len(ctx["symbols"]) == 5 and all(len(s["history"]) == 5 and len(s["reasons"]) == 3 for s in ctx["symbols"])
    assert ctx["approx_tokens"] <= 4000, ctx["approx_tokens"]


def test_ledger_matches_does_not_tokenise_anchors(ledger_db):
    """Phase 5: a constraint's dotted anchors (qualified names, owner.column,
    nk_ keys) are exact anchors, not keywords — a plan in the same module must
    not be told it 'read' the constraint because it shares 'pkg' / 'pipeline'."""
    c = sqlite3.connect(ledger_db); c.row_factory = sqlite3.Row
    cid = ledger_store.add_entry(
        c, project="proj", kind="constraint", statement="clean() must keep dropping zero-quantity rows",
        subjects=["pkg.pipeline.clean", "nk_290532fc7c77"], keywords=["scope-change"], source="extensions")
    c.commit(); c.close()
    same_module = impact_preflight.ledger_matches(ledger_db, "proj", "touch the split", ["pkg.pipeline.split"])
    assert cid not in [m["id"] for m in same_module]
    by_keyword = impact_preflight.ledger_matches(ledger_db, "proj", "a scope-change of the filter", ["pkg.pipeline.split"])
    assert cid in [m["id"] for m in by_keyword]                     # keywords still match lexically
