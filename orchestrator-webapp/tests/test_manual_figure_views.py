"""A figure nobody can trace says so, on every surface that shows it
(DP phase 4, Task 2; spec §9 and §18).

A `manual_figure` is a number somebody computed by hand. The honest move is not
to refuse it and not to let it sit next to measured metrics looking the same,
but to carry the truth with it: the node page and the outcomes ledger both
print "no traceable data source" beside it. The dashboard stays read-only; it
prints what the ledger already says.
"""
from __future__ import annotations

import importlib
import json
import sqlite3
from pathlib import Path

import pytest

from test_routes import _seed_db, _seed_state_graph  # noqa: E402

FIGURE_QN = "declared:q3-conv-manual"


def _seed_manual_figure(graph_path: Path) -> None:
    """The declared stage of an analysis run projects the 026 row into the graph."""
    meta = {"declared_id": 1, "slug": "q3-conv-manual", "node_type": "manual_figure", "state": "active",
            "tier": "stated", "version": 2, "description": "finance worked it out from the raw export",
            "attrs": {"value": "3.2", "note": "finance worked it out from the raw export"},
            "links": [], "links_checked": 1, "field_tiers": {"node_type": "stated", "links": []}}
    c = sqlite3.connect(graph_path)
    c.executescript("INSERT INTO node_type (id, name) VALUES (32, 'manual_figure');")
    c.execute("INSERT INTO node (id, node_type_id, name, qualified_name, metadata_json, run_id, node_key) "
              "VALUES (32, 32, ?, ?, ?, 2, 'nk_fig')", (meta["slug"], FIGURE_QN, json.dumps(meta)))
    c.execute("INSERT INTO node_snapshot (run_id, node_key, node_type, qualified_name, file_path, struct_sig, "
              "dataflow_sig, dataflow_trivial, attrs_json) VALUES (2, 'nk_fig', 'manual_figure', ?, NULL, 's', 'd', 0, ?)",
              (FIGURE_QN, json.dumps({"type_id": "provledger.declared", "declared_tier": "stated",
                                      "declared_attrs": meta["attrs"], "declared_links": [],
                                      "declared_version": 2})))
    c.execute("INSERT INTO node_event (run_id, seq, event_type, node_key, tier, payload_json, created_at) "
              "VALUES (2, 60, 'node_added', 'nk_fig', 'observed', '{}', '2026-09-17T00:00:00+00:00')")
    c.commit()
    c.close()


def _seed_claim(db_path: Path) -> None:
    """The 026 row itself plus an expectation whose target is that figure —
    somebody promised a number they cannot trace, which is precisely the row
    worth marking. The declared node goes in through its own store, so what the
    page reads is what `node add --manual-figure` would have written."""
    from orchestrator import db as odb, declared, provenance
    conn = odb.open_db(db_path)
    draft = declared.declare(conn, "demo", "finance worked it out from the raw export",
                             node_type="manual_figure", name="q3_conv_manual",
                             attrs={"value": "3.2", "note": "finance worked it out from the raw export"})
    uid = provenance.insert_utterance(conn, session_id="s", project="demo", plan_id=None,
                                      text="finance worked it out from the raw export",
                                      occurred_at="2026-09-17 09:00:00")
    declared.confirm(conn, draft["id"], uid)
    conn.execute("INSERT INTO expectations (plan_id, step_id, project, target, target_kind, claim, channel, created_at) "
                 "VALUES ('P1', 'P1-A', 'demo', ?, 'node', 'Q3 conversion lands at 3.2%', 'manual', "
                 "'2026-09-17 09:00:00')", (FIGURE_QN,))
    conn.commit()
    conn.close()


@pytest.fixture
def client(tmp_path, monkeypatch):
    dbp = tmp_path / "orch.db"
    _seed_db(dbp)
    _seed_claim(dbp)
    monkeypatch.setenv("ORCH_DB", str(dbp))
    graph, reg = _seed_state_graph(tmp_path)
    _seed_manual_figure(graph)
    monkeypatch.setenv("PSG_REGISTRY_PATH", str(reg))
    from app import main, queries
    importlib.reload(queries)
    importlib.reload(main)
    from fastapi.testclient import TestClient
    return TestClient(main.app)


def test_the_node_page_says_a_manual_figure_has_no_traceable_source(client):
    html = client.get(f"/node/demo/{FIGURE_QN}").text
    assert 'data-no-source="1"' in html, "a hand-computed figure must say so on its own page"
    assert "no traceable data source" in html
    assert 'data-declared-type="manual_figure"' in html
    assert 'data-declared-tier="stated"' in html          # the person typed the number


def test_the_outcomes_ledger_carries_the_same_marker(client):
    html = client.get("/outcomes").text
    assert FIGURE_QN in html
    assert 'data-no-source="1"' in html
    assert "no traceable data source" in html


def test_a_measured_metric_carries_no_such_marker(client):
    """The marker has to mean something, so it may not appear on a node whose
    number came from somewhere."""
    html = client.get("/node/demo/pkg.m.load_orders").text
    assert 'data-no-source="1"' not in html
