"""The node page says where this number turned up (DP phase 4, Task 3; spec §9).

The Occurrences section answers a question the rest of the page cannot: this
metric is 3.2 — where has anybody actually printed it, and does that place
still say so? Each row names the file, the place inside it, the value, when it
was seen and what the last check found.

Two things this section is careful about.

The file path is **text, not a link**. The dashboard is read-only and lives in
a browser; a link to somebody's deck either does nothing or opens a file:// URL
that promises more than it can deliver.

And the section renders for a node the state graph has never heard of. A figure
in a deck is a reading of `metric:q3_conv`, and that identity exists the moment
somebody records the metric — no analysis run has to bless it first. A section
that only appeared for nodes in the graph would quietly say "this metric does
not exist" about every metric in the project.
"""
from __future__ import annotations

import importlib
import json
import sqlite3
from pathlib import Path

import pytest

from test_routes import _seed_db, _seed_state_graph  # noqa: E402

METRIC = "metric:q3_conv"


def _seed_occurrences(db_path: Path) -> None:
    """Two anchors on one node, written the way `provledger anchor` writes them:
    one still ok, one lost because the deck was revised under it."""
    from orchestrator import db as odb, provenance
    conn = odb.open_db(db_path)
    conn.execute("INSERT INTO metrics (project, name, value, unit, source) "
                 "VALUES ('demo', 'q3_conv', 3.2, 'percent', 'record-metric')")
    for path, sha in (("decks/q3.pptx", "a" * 64), ("reports/board.docx", "b" * 64)):
        conn.execute("INSERT INTO artifact_file (project, path, sha256, kind) VALUES ('demo', ?, ?, ?)",
                     (path, sha, "pptx" if path.endswith("pptx") else "docx"))
    for file_id, locator, seen in (
            (1, {"kind": "pptx", "slide": 4, "shape": 2, "at": "slide 4"}, "2026-09-17 09:00:00"),
            (2, {"kind": "docx", "paragraph": 3, "at": "paragraph 3"}, "2026-09-17 09:30:00")):
        provenance._insert_chained(conn, "occurrence", {
            "project": "demo", "node_key": METRIC, "file_id": file_id,
            "locator_json": json.dumps(locator, sort_keys=True), "value_text": "3.2", "value_num": 3.2,
            "seen_at": seen, "tier": "observed", "by": "human", "recorded_at": seen})
    conn.execute("INSERT INTO anchor_state (occurrence_id, state, checked_at, reason) "
                 "VALUES (1, 'ok', '2026-09-17 10:00:00', NULL)")
    conn.execute("INSERT INTO anchor_state (occurrence_id, state, checked_at, reason) "
                 "VALUES (2, 'anchor_lost', '2026-09-17 10:00:00', "
                 "'3.2 is no longer at paragraph 3: moved or removed')")
    conn.execute("INSERT INTO expectations (plan_id, step_id, project, target, target_kind, claim, channel, created_at) "
                 "VALUES ('P1', 'P1-A', 'demo', ?, 'metric', 'Q3 conversion holds at 3.2%', 'metric', "
                 "'2026-09-17 09:00:00')", (METRIC,))
    conn.commit()
    conn.close()


@pytest.fixture
def client(tmp_path, monkeypatch):
    dbp = tmp_path / "orch.db"
    _seed_db(dbp)
    _seed_occurrences(dbp)
    monkeypatch.setenv("ORCH_DB", str(dbp))
    graph, reg = _seed_state_graph(tmp_path)
    monkeypatch.setenv("PSG_REGISTRY_PATH", str(reg))
    from app import main, queries
    importlib.reload(queries)
    importlib.reload(main)
    from fastapi.testclient import TestClient
    return TestClient(main.app)


def test_the_node_page_lists_where_the_number_turned_up(client):
    html = client.get(f"/node/demo/{METRIC}").text
    assert 'data-panel="occurrences"' in html
    assert "Occurrences" in html
    assert "decks/q3.pptx" in html and "reports/board.docx" in html
    assert "slide 4" in html and "paragraph 3" in html
    assert "2026-09-17 09:00:00" in html
    assert 'data-occurrence="1"' in html and 'data-occurrence="2"' in html


def test_the_section_renders_for_a_node_the_state_graph_never_heard_of(client):
    """`metric:q3_conv` is not a symbol in any snapshot, and it does not have to
    be: its identity is the data source, not the analyser's opinion of it."""
    r = client.get(f"/node/demo/{METRIC}")
    assert r.status_code == 200
    assert 'data-state="not-found"' in r.text            # the graph half says so, honestly
    assert 'data-panel="occurrences"' in r.text          # and the occurrences are still shown


def test_a_lost_anchor_is_marked_lost_with_its_reason(client):
    html = client.get(f"/node/demo/{METRIC}").text
    assert 'data-anchor-state="ok"' in html
    assert 'data-anchor-state="anchor_lost"' in html
    assert "moved or removed" in html
    assert "never re-pointed" in html                    # the page says what lost means


def test_the_file_path_is_text_and_not_a_link(client):
    html = client.get(f"/node/demo/{METRIC}").text
    assert 'href="decks/q3.pptx"' not in html and "file://" not in html


def test_the_outcomes_row_links_to_the_places_the_number_was_seen(client):
    html = client.get("/outcomes").text
    assert f'/node/demo/{METRIC}' in html
    assert 'data-occurrences="2"' in html


def test_a_node_with_no_anchors_says_so_rather_than_hiding_the_section(client):
    html = client.get("/node/demo/pkg.m.load_orders").text
    assert "no file is anchored to this node yet" in html
