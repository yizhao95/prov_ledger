"""I12 — the context triple (project, node, at) survives a round trip Graph → Node → Task → Graph (DP phase 2b, Task 4)."""
from __future__ import annotations

import html as _html
import importlib
import json
import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
ORCH_BACKEND = REPO / "orchestrator-backend"          # the bundled package, the way test_routes reaches it (never spliced on one line — the PSG host-import rule)
sys.path.insert(0, str(ORCH_BACKEND))
sys.path.insert(0, str(REPO / "orchestrator-webapp" / "tests"))

from orchestrator import db as odb  # noqa: E402
from test_routes import _seed_db, _seed_state_graph, _seed_reasons_and_constraints  # noqa: E402


@pytest.fixture
def client(tmp_path, monkeypatch):
    dbp = tmp_path / "orch.db"
    seeded = _seed_db(dbp)
    monkeypatch.setenv("ORCH_DB", str(dbp))
    _, reg = _seed_state_graph(tmp_path)
    monkeypatch.setenv("PSG_REGISTRY_PATH", str(reg))
    _seed_reasons_and_constraints(dbp)
    from app import queries, main
    importlib.reload(queries); importlib.reload(main)
    from fastapi.testclient import TestClient
    c = TestClient(main.app); c._seeded = seeded; c._db = dbp
    return c


def _bar(html: str) -> dict:
    m = re.search(r'<nav class="[^"]*" data-view-bar data-view="([^"]*)"\s+data-project="([^"]*)" data-node="([^"]*)" data-at="([^"]*)"', html)
    assert m, "no view bar"
    links = dict(re.findall(r'<a href="([^"]+)" data-view-link="(\w+)"', html))
    return {"view": m.group(1), "project": m.group(2), "node": m.group(3), "at": m.group(4),
            "links": {v: _html.unescape(h) for h, v in links.items()}}          # attributes are HTML-escaped (&amp;) — the browser decodes them


def test_round_trip_keeps_the_triple(client):
    pid = client._seeded["plan_id"]
    conn = odb.open_db(client._db); conn.execute("UPDATE Plans SET project='demo' WHERE plan_id=?", (pid,)); conn.commit(); conn.close()
    g = _bar(client.get("/graph/demo?focus=pkg.m.load_orders&at=2").text)
    assert g["view"] == "graph" and (g["project"], g["node"], g["at"]) == ("demo", "pkg.m.load_orders", "2")
    assert g["links"]["node"] == "/node/demo/pkg.m.load_orders?at=2"
    n = _bar(client.get(g["links"]["node"]).text)
    assert n["view"] == "node" and (n["project"], n["node"], n["at"]) == ("demo", "pkg.m.load_orders", "2")
    assert n["links"]["graph"] == "/graph/demo?focus=pkg.m.load_orders&at=2"
    # Task: the node page knows the plans that touched the node; follow its Task link with the node carried along
    t_url = f"/plan/{pid}?node=pkg.m.load_orders&at=2"
    t = _bar(client.get(t_url).text)
    assert t["view"] == "task" and (t["project"], t["node"], t["at"]) == ("demo", "pkg.m.load_orders", "2")
    assert t["links"]["graph"] == "/graph/demo?focus=pkg.m.load_orders&at=2" and t["links"]["node"] == "/node/demo/pkg.m.load_orders?at=2"
    back = _bar(client.get(t["links"]["graph"]).text)
    assert (back["project"], back["node"], back["at"]) == ("demo", "pkg.m.load_orders", "2")     # the round trip loses nothing


def test_task_view_highlights_the_focused_node(client):
    pid = client._seeded["plan_id"]
    conn = odb.open_db(client._db)
    conn.execute("UPDATE Plans SET project='demo' WHERE plan_id=?", (pid,))
    conn.execute("INSERT INTO change_reason (project, plan_id, node_key, kind, role, interpretation, occurred_at, recorded_by, tier, hash) VALUES ('demo', ?, 'nk_a', 'technical', 'reason', 'x', '2026-09-16 00:00:00', 'agent', 'asserted', 'h')", (pid,))
    conn.commit(); conn.close()
    html = client.get(f"/plan/{pid}?node=nk_a").text
    assert 'data-focus="1"' in html and _bar(html)["node"] == "nk_a"
    plain = client.get(f"/plan/{pid}").text
    assert 'data-focus="1"' not in plain and _bar(plain)["node"] == ""


def test_missing_at_means_latest_and_the_bar_carries_no_at(client):
    n = _bar(client.get("/node/demo/pkg.m.load_orders").text)
    assert n["at"] == "" and n["links"]["graph"] == "/graph/demo?focus=pkg.m.load_orders"
    html = client.get("/node/demo/pkg.m.load_orders?at=1").text
    assert 'data-at-run="1"' in html                                       # the timeline highlights run 1


def test_bar_survives_a_missing_graph(client, tmp_path, monkeypatch):
    monkeypatch.setenv("PSG_REGISTRY_PATH", str(tmp_path / "none.json")); (tmp_path / "none.json").write_text('{"projects": []}')
    html = client.get("/graph/demo?focus=x&at=3").text
    assert 'data-state="unavailable"' in html
    b = _bar(html)
    assert b["view"] == "graph" and b["node"] == "x" and b["at"] == "3" and b["links"]["node"] == "/node/demo/x?at=3"
