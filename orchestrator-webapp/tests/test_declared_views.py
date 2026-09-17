"""A declared node is visible as such in all three views (DP phase 2c, spec §18).

The marker answers a question the reader would otherwise have to guess at: this
node is not code, somebody said it. Beside the marker sits the DECLARATION's
tier — stated when the user's own words put it there, asserted when a model
tidied it — next to, never instead of, the event tier the graph computed.
"""
from __future__ import annotations

import importlib
import json
import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "orchestrator-backend"))
sys.path.insert(0, str(REPO / "orchestrator-webapp"))          # FL-077: `app` must not depend on collection order
sys.path.insert(0, str(REPO / "orchestrator-webapp" / "tests"))

from orchestrator import db as odb  # noqa: E402
from test_routes import _seed_db, _seed_state_graph  # noqa: E402

RULE_QN = "declared:emea-excluded-from-the-q3-rollup"
FEED_QN = "declared:salesforce-export-feed"


def _seed_declared(graph_path):
    """Two declared nodes the analyzer's declared stage would have written: a
    rule that constrains pkg.m.load_orders and an external system that feeds it."""
    import sqlite3
    c = sqlite3.connect(graph_path)
    meta_rule = {"declared_id": 1, "slug": "emea-excluded-from-the-q3-rollup", "node_type": "business_rule",
                 "state": "active", "tier": "stated", "version": 2, "description": "EMEA excluded from the Q3 rollup",
                 "attrs": {"decided_on": "2026-03-14"},
                 "links": [{"to": "pkg.m.load_orders", "kind": "declared_constrains", "by": "user"}],
                 "links_checked": 1, "field_tiers": {"node_type": "stated", "links": ["stated"]}}
    meta_feed = {"declared_id": 2, "slug": "salesforce-export-feed", "node_type": "external_system",
                 "state": "active", "tier": "asserted", "version": 2, "description": "the Salesforce export feed",
                 "attrs": {}, "links": [{"to": "pkg.m.load_orders", "kind": "declared_feeds", "by": "model"}],
                 "links_checked": 1, "field_tiers": {"node_type": "asserted", "links": ["asserted"]}}
    c.executescript("""
        CREATE TABLE IF NOT EXISTS edge_type (id INTEGER PRIMARY KEY, name TEXT UNIQUE, description TEXT);
        CREATE TABLE IF NOT EXISTS edge (id INTEGER PRIMARY KEY, edge_type_id INTEGER, src_node_id INTEGER,
                                         dst_node_id INTEGER, metadata_json TEXT, confidence TEXT, run_id INTEGER);
        INSERT INTO node_type (id, name) VALUES (30, 'business_rule'), (31, 'external_system');
        INSERT INTO edge_type (id, name) VALUES (40, 'declared_constrains'), (41, 'declared_feeds');
    """)
    for nid, tid, meta, key in ((30, 30, meta_rule, "nk_rule"), (31, 31, meta_feed, "nk_feed")):
        qn = "declared:" + meta["slug"]
        c.execute("INSERT INTO node (id, node_type_id, name, qualified_name, metadata_json, run_id, node_key) "
                  "VALUES (?, ?, ?, ?, ?, 2, ?)", (nid, tid, meta["slug"], qn, json.dumps(meta), key))
        c.execute("INSERT INTO node_snapshot (run_id, node_key, node_type, qualified_name, file_path, "
                  "struct_sig, dataflow_sig, dataflow_trivial, attrs_json) VALUES (2, ?, ?, ?, NULL, 's', 'd', 0, ?)",
                  (key, meta["node_type"], qn, json.dumps({"type_id": "provledger.declared",
                                                           "declared_tier": meta["tier"],
                                                           "declared_attrs": meta["attrs"],
                                                           "declared_links": [f"{d['kind']}:{d['to']}" for d in meta["links"]],
                                                           "declared_version": meta["version"]})))
        c.execute("INSERT INTO node_event (run_id, seq, event_type, node_key, tier, payload_json, created_at) "
                  "VALUES (2, 50, 'node_added', ?, 'observed', '{}', '2026-09-11T00:00:00+00:00')", (key,))
        c.execute("INSERT INTO edge (edge_type_id, src_node_id, dst_node_id) VALUES (?, ?, 7)",
                  (40 if meta["node_type"] == "business_rule" else 41, nid))
    c.commit()
    c.close()


@pytest.fixture
def client(tmp_path, monkeypatch):
    dbp = tmp_path / "orch.db"
    _seed_db(dbp)
    monkeypatch.setenv("ORCH_DB", str(dbp))
    graph, reg = _seed_state_graph(tmp_path)
    _seed_declared(graph)
    monkeypatch.setenv("PSG_REGISTRY_PATH", str(reg))
    from app import main, queries
    importlib.reload(queries)
    importlib.reload(main)
    from fastapi.testclient import TestClient
    return TestClient(main.app)


def test_the_node_page_marks_a_declared_node_and_names_its_declaration_tier(client):
    html = client.get(f"/node/demo/{RULE_QN}").text
    assert 'data-declared="1"' in html, "a declared node must say so on its own page"
    assert re.search(r'data-declared-tier="stated"', html)
    assert "EMEA excluded from the Q3 rollup" in html
    assert 'data-declared-type="business_rule"' in html
    # the declaration tier is printed BESIDE the event tier, not instead of it
    assert 'data-tier="observed"' in html


def test_a_code_node_carries_no_declared_marker(client):
    html = client.get("/node/demo/pkg.m.load_orders").text
    assert 'data-declared="1"' not in html


def test_the_graph_marks_declared_nodes_and_puts_an_external_system_at_level_zero(client):
    html = client.get("/graph/demo?level=full&mode=full").text
    payload = json.loads(re.search(r"var data = (\{.*?\});\n", html, re.S).group(1))
    by_qn = {n["qualified_name"]: n for n in payload["nodes"]}
    assert RULE_QN in by_qn and FEED_QN in by_qn, sorted(by_qn)
    assert by_qn[FEED_QN]["declared"] is True and by_qn[RULE_QN]["declared"] is True
    assert by_qn[FEED_QN]["level"] == 0, "an external system is where something comes from"
    assert by_qn[RULE_QN]["lane"] == "constraint", "a rule sits beside what it governs, not above it"
    assert by_qn["pkg.m.load_orders"]["lane"] == "graph"


def test_the_graph_page_lists_the_declared_constraint_lane_with_a_link_to_each_target(client):
    html = client.get("/graph/demo?level=full&mode=full").text
    assert 'data-panel="declared-lane"' in html
    lane = html.split('data-panel="declared-lane"', 1)[1].split("</section>", 1)[0]
    assert RULE_QN in lane and "stated" in lane
    assert f'/node/demo/{RULE_QN}' in lane
    assert "pkg.m.load_orders" in lane, "the lane must name what the rule constrains"


def test_the_graph_table_marks_declared_rows(client):
    """The node table is data as much as it is a page: a declared row must be
    findable by attribute, not by reading the label."""
    html = client.get("/graph/demo?level=full&mode=full").text
    table = html.split('data-panel="graph-table"', 1)[1]
    rows = [r for r in table.split("<tr ")[1:] if RULE_QN in r.split("</tr>", 1)[0]]
    assert rows, "the declared rule is not in the node table"
    assert 'data-declared="1"' in rows[0] and 'data-lane="constraint"' in rows[0]
    code = [r for r in table.split("<tr ")[1:] if "pkg.m.load_orders" in r.split("</tr>", 1)[0]]
    assert code and 'data-declared="1"' not in code[0]


def test_declared_types_are_in_the_vocabulary_in_both_languages(client):
    from app import vocab
    for token in ("external_system", "business_rule", "stakeholder_decision", "external_dataset", "manual_figure"):
        for lang in ("en", "zh"):
            phrase = vocab.say(token, kind="declared_type", lang=lang)
            assert phrase and vocab.is_fallback(token, kind="declared_type", lang=lang) is False, (token, lang)
    en = {vocab.say(t, kind="declared_type", lang="en") for t in
          ("external_system", "business_rule", "stakeholder_decision", "external_dataset", "manual_figure")}
    assert len(en) == 5, "five kinds of real-world object must still read as five different things"
