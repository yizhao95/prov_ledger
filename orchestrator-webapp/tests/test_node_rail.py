"""One decision, one row (DP phase 2d, final rail model).

A decision record — a constraint, a reason, a rejected alternative — appears on
the node's timeline ONCE. Every later activation of it (a plan's close, a
publish, an edit, a `why` query) raises its hit count; it does not produce
another line of text. Rules therefore never insert a second reason, and the 16
identical derived rows already on `compute_etag` are merged in the READ layer,
with the merge stated in the row's tooltip rather than hidden.

That leaves two kinds of row and nothing else:

  changes    observed events — added / changed / renamed / moved / removed /
             column dropped / identity asserted. Secondary, one line.
  decisions  what somebody decided. The text is the subject of the row.

And one thing lit at a time: only the row the URL points at gets a filled dot,
a tint and a +1.
"""
from __future__ import annotations

import importlib
import json
import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
ORCH_BACKEND = REPO / "orchestrator-backend"
WEBAPP = REPO / "orchestrator-webapp"
sys.path.insert(0, str(WEBAPP))
sys.path.insert(0, str(ORCH_BACKEND))
sys.path.insert(0, str(ORCH_BACKEND / "tests"))
sys.path.insert(0, str(REPO / "orchestrator-webapp" / "tests"))

import _psg_schema as ps  # noqa: E402
from orchestrator import db as odb  # noqa: E402
from test_routes import _seed_db  # noqa: E402

QN = "pkg.m.compute_etag"
DUPES = 16          # the same derived sentence, once per plan that met the node
EVENTS = 3          # added, changed, changed
DECISIONS = 4       # 1 constraint + 2 asserted + 1 merged derived
CONSTRAINT_TEXT = "the dashboard ETag must change whenever a close-time row lands"
DERIVED_TEXT = "covered by active constraint #1414: " + CONSTRAINT_TEXT


def _graph(tmp_path, project="demo"):
    path = tmp_path / f"{project}-state-graph.db"
    c = ps.build(path)
    ps.add_run(c, 1, plan_id="P0", sha="aaaaaaa")
    ps.add_snapshot(c, 1, "nk_a", QN)
    ps.add_event(c, 1, 1, "node_added", "nk_a", '{"qualified_name": "%s"}' % QN)
    ps.add_run(c, 2, plan_id="P1", sha="bbbbbbb")
    ps.add_snapshot(c, 2, "nk_a", QN, struct_sig="s2")
    ps.add_event(c, 2, 1, "node_changed", "nk_a", '{"changed": ["struct_sig", "dataflow_sig"]}')
    ps.add_run(c, 3, plan_id="P2", sha="ccccccc")
    ps.add_snapshot(c, 3, "nk_a", QN, struct_sig="s3")
    ps.add_event(c, 3, 1, "node_changed", "nk_a", '{"changed": ["struct_sig"]}')
    c.executescript(f"""
        CREATE TABLE IF NOT EXISTS consistency_card (symbol_id INTEGER PRIMARY KEY, card_json TEXT NOT NULL);
        INSERT INTO node_type (id, name) VALUES (1, 'function');
        INSERT INTO node (id, node_type_id, name, qualified_name, file_path, run_id, node_key)
             VALUES (7, 1, 'compute_etag', '{QN}', 'app/queries.py', 3, 'nk_a');
        INSERT INTO consistency_card VALUES (7, '{{"callers": ["app.main.dashboard"], "callees": [], "output_consumers": ["app.main.api"], "reads": [], "writes": []}}');
    """)
    c.commit(); c.close()
    reg = tmp_path / "projects.json"
    reg.write_text(json.dumps({"projects": [{"name": project, "repo": str(tmp_path), "db_path": str(path), "commit_sha": "ccccccc"}]}))
    return str(path), reg


def _seed(dbp, project="demo"):
    """The shape the real page had: one constraint, two asserted reasons, and the
    same derived sentence written once per plan that met the node."""
    from orchestrator import constraints as oc, provenance as pv
    conn = odb.open_db(dbp)
    oc.record_constraint(conn, project=project, subjects=["nk_a"], statement=CONSTRAINT_TEXT,
                         rationale="a stale ETag hides a new row", why_ref="docs/decision-provenance.md",
                         why_visibility="shared")
    for i, text in enumerate(("compute_etag also hashes MAX(id) of node_reason and outcomes",
                              "the hash covers outcomes so a backfilled result invalidates it")):
        pv.insert_reason(conn, project=project, plan_id=f"A{i}", node_key="nk_a", kind="technical",
                         run_id=3, interpretation=text, recorded_by="agent", commit=False)
    for i in range(DUPES):
        pv.insert_reason(conn, project=project, plan_id=f"R{i}", node_key="nk_a", kind="technical",
                         run_id=3, interpretation=DERIVED_TEXT, rule_id="R5", recorded_by="system", commit=False)
    conn.commit()
    cid = conn.execute("SELECT id FROM change_reason WHERE role = 'constraint'").fetchone()[0]
    for moment in ("plan", "edit", "why"):
        conn.execute("INSERT INTO read_hit (reason_id, project, plan_id, moment) VALUES (?, ?, 'P9', ?)",
                     (cid, project, moment))
    conn.commit(); conn.close()
    return cid


@pytest.fixture
def client(tmp_path, monkeypatch):
    dbp = tmp_path / "orch.db"
    _seed_db(dbp)
    monkeypatch.setenv("ORCH_DB", str(dbp))
    _, reg = _graph(tmp_path)
    monkeypatch.setenv("PSG_REGISTRY_PATH", str(reg))
    cid = _seed(dbp)
    from app import queries, main
    importlib.reload(queries); importlib.reload(main)
    from fastapi.testclient import TestClient
    c = TestClient(main.app); c._db = dbp; c._cid = cid
    return c


def _rail(client, q=""):
    return client.get(f"/node/demo/{QN}{q}").text


# ── one decision, one row ────────────────────────────────────────────────────

def test_the_rail_is_exactly_the_changes_plus_the_distinct_decisions(client):
    t = _rail(client)
    rows = re.findall(r'data-row="(change|decision)"', t)
    assert len(rows) == EVENTS + DECISIONS, f"{len(rows)} rail rows: {rows}"
    assert rows.count("change") == EVENTS and rows.count("decision") == DECISIONS


def test_the_repeated_sentence_appears_once(client):
    """Once as VISIBLE text. It also rides in the row's title= so a truncated
    line can be read in full on hover — that is the tooltip doing its job, not
    the page repeating itself."""
    body = re.sub(r"<[^>]*>", " ", _rail(client))
    assert body.count(DERIVED_TEXT) == 1, "the merged sentence is still printed more than once"


def test_hits_are_the_activations_not_the_rows(client):
    """A record's hit count is how often it was surfaced: its read_hit rows plus
    every duplicate write, because each of those was an activation too."""
    from app import queries
    conn = queries.open_db_readonly(client._db)
    try:
        led = queries.get_node_ledger(conn, "demo", QN)
    finally:
        conn.close()
    merged = next(r for r in led["rail"] if r.get("text") == DERIVED_TEXT)
    assert merged["hits"] >= DUPES, f"merged hits {merged['hits']} < {DUPES} activations"
    assert merged["merged"] == DUPES - 1   # 16 writes, 15 folded into the first
    constraint = next(r for r in led["rail"] if r.get("text") == CONSTRAINT_TEXT)
    assert constraint["hits"] == 3, "the constraint's three read_hit rows are its hits"


def test_the_merge_is_stated_not_hidden(client):
    t = _rail(client)
    assert f"{DUPES}" in t
    assert re.search(r'title="[^"]*merged[^"]*"', t, re.I), "the merge is not explained anywhere"


# ── exactly one thing lit ────────────────────────────────────────────────────

def test_only_the_row_the_url_points_at_is_lit(client):
    t = _rail(client, f"?at=reason:{client._cid}")
    assert t.count("data-hit-here") == 1, f"{t.count('data-hit-here')} rows lit at once"
    assert "+1" in t


def test_nothing_is_lit_without_an_anchor(client):
    assert "data-hit-here" not in _rail(client)


# ── the visual hierarchy ─────────────────────────────────────────────────────

def test_changes_are_secondary_and_decisions_carry_the_text(client):
    t = _rail(client)
    change = re.search(r'<div[^>]*data-row="change"[^>]*class="([^"]*)"', t) or \
             re.search(r'<div[^>]*class="([^"]*)"[^>]*data-row="change"', t)
    decision = re.search(r'<div[^>]*data-row="decision"[^>]*class="([^"]*)"', t) or \
               re.search(r'<div[^>]*class="([^"]*)"[^>]*data-row="decision"', t)
    assert change and decision
    assert "brand-muted" in change.group(1), "change rows are not the secondary colour"


def test_open_task_is_not_resident(client):
    """A link on every row is a link nobody reads. It appears on hover or when
    the row is expanded — never as standing prose."""
    t = _rail(client)
    visible = re.sub(r'<[^>]*(group-hover|hidden)[^>]*>.*?</a>', " ", t, flags=re.S)
    prose = re.sub(r"<[^>]*>", " ", visible)
    assert "Open task" not in prose


def test_the_recent_block_is_gone(client):
    t = _rail(client)
    assert 'data-panel="trace-strip"' not in t and "Recent" not in re.sub(r"<[^>]*>", " ", t)


def test_unchanged_matches_still_fold_with_a_count(client):
    from app import queries
    conn = queries.open_db_readonly(client._db)
    try:
        led = queries.get_node_ledger(conn, "demo", QN)
    finally:
        conn.close()
    assert "folded" in led


def test_the_constraints_aside_marks_the_same_row(client):
    t = _rail(client, f"?at=reason:{client._cid}")
    aside = t[t.index('data-panel="constraints"'):]
    assert 'data-at="1"' in aside or "data-hit-here" in aside
