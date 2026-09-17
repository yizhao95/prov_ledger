"""DP phase 2d (Task 0): the context triple's `at` says WHAT it points at.

2b shipped a bare `at`: Node used it for a run id AND for a reason id, Graph
assumed a run. An "adopted by <plan>" link carries a reason id, so Graph silently
looked for run 1414. From 2d on, `at` is `run:<id>` or `reason:<id>`; a bare
number still means a run for one version (compatibility), and a non-numeric
`at` is still a plan id (the Task view's anchor).
"""
from __future__ import annotations

import html as _html
import importlib
import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
ORCH_BACKEND = REPO / "orchestrator-backend"
WEBAPP = REPO / "orchestrator-webapp"                  # `app` on the path without relying on collection order
sys.path.insert(0, str(WEBAPP))
sys.path.insert(0, str(ORCH_BACKEND))
sys.path.insert(0, str(REPO / "orchestrator-webapp" / "tests"))

from test_routes import _seed_db, _seed_state_graph, _seed_reasons_and_constraints  # noqa: E402


@pytest.fixture
def client(tmp_path, monkeypatch):
    dbp = tmp_path / "orch.db"
    _seed_db(dbp)
    monkeypatch.setenv("ORCH_DB", str(dbp))
    _, reg = _seed_state_graph(tmp_path)
    monkeypatch.setenv("PSG_REGISTRY_PATH", str(reg))
    _seed_reasons_and_constraints(dbp)
    from app import queries, main
    importlib.reload(queries); importlib.reload(main)
    from fastapi.testclient import TestClient
    c = TestClient(main.app); c._db = dbp
    return c


@pytest.fixture
def q():
    from app import queries
    return queries


def _reason_ids(dbp):
    import sqlite3
    return [r[0] for r in sqlite3.connect(str(dbp)).execute(
        "SELECT id FROM change_reason WHERE node_key = 'nk_a' ORDER BY id")]


# ── the parser ───────────────────────────────────────────────────────────────

def test_parse_at_types_the_two_prefixes_and_keeps_a_bare_number_as_a_run(q):
    assert q.parse_at("run:70") == {"kind": "run", "id": 70, "at": "run:70"}
    assert q.parse_at("reason:1414") == {"kind": "reason", "id": 1414, "at": "reason:1414"}
    assert q.parse_at("70") == {"kind": "run", "id": 70, "at": "run:70"}          # one version of compatibility
    assert q.parse_at(70) == {"kind": "run", "id": 70, "at": "run:70"}


def test_parse_at_never_guesses_for_an_unprefixed_non_number(q):
    """A plan id is a legitimate `at` for the Task view — it is simply untyped."""
    assert q.parse_at("dp2b-t5-20260916075102") == {"kind": None, "id": None, "at": "dp2b-t5-20260916075102"}
    assert q.parse_at(None) == {"kind": None, "id": None, "at": None}
    assert q.parse_at("") == {"kind": None, "id": None, "at": None}
    assert q.parse_at("run:") == {"kind": None, "id": None, "at": "run:"}
    assert q.parse_at("reason:abc") == {"kind": None, "id": None, "at": "reason:abc"}


def test_triple_carries_the_kind_and_the_id(q):
    t = q.triple("prov_ledger", "pkg.m.load", "reason:12")
    assert (t["at"], t["at_kind"], t["at_id"]) == ("reason:12", "reason", 12)
    t2 = q.triple("prov_ledger", "pkg.m.load", "70")
    assert (t2["at"], t2["at_kind"], t2["at_id"]) == ("run:70", "run", 70)
    t3 = q.triple("prov_ledger", None, None)
    assert (t3["at"], t3["at_kind"], t3["at_id"]) == (None, None, None)


# ── the links every view emits ───────────────────────────────────────────────

def test_url_for_view_emits_the_prefixed_at_for_graph_and_node(q):
    t = q.triple("demo", "pkg.m.load_orders", "run:2")
    assert q.url_for_view("graph", t) == "/graph/demo?focus=pkg.m.load_orders&at=run%3A2"
    assert q.url_for_view("node", t) == "/node/demo/pkg.m.load_orders?at=run%3A2"
    r = q.triple("demo", "pkg.m.load_orders", "reason:9")
    assert q.url_for_view("graph", r) == "/graph/demo?focus=pkg.m.load_orders&at=reason%3A9"
    assert q.url_for_view("node", r) == "/node/demo/pkg.m.load_orders?at=reason%3A9"


def test_a_bare_run_number_is_normalised_into_the_links(q):
    t = q.triple("demo", "pkg.m.load_orders", "2")
    assert "at=run%3A2" in q.url_for_view("graph", t) and "at=run%3A2" in q.url_for_view("node", t)


def test_an_untyped_at_is_still_the_task_anchor(q):
    t = q.triple("demo", "pkg.m.load_orders", "P1")
    assert q.url_for_view("task", t) == "/plan/P1?node=pkg.m.load_orders"
    assert q.url_for_view("graph", t) == "/graph/demo?focus=pkg.m.load_orders"     # no run to go to — absent, not guessed


# ── the three views ──────────────────────────────────────────────────────────

def test_node_page_highlights_the_record_for_reason_and_the_run_for_run(client):
    rid = _reason_ids(client._db)[0]
    t = client.get(f"/node/demo/pkg.m.load_orders?at=reason:{rid}").text
    assert f'data-record="{rid}"' in t and 'data-at="1"' in t
    assert "data-at-run=" not in t                                       # a reason id is not a run id
    t2 = client.get("/node/demo/pkg.m.load_orders?at=run:2").text
    assert 'data-at-run="2"' in t2 and 'data-at="1"' not in t2


def test_a_bare_at_still_highlights_the_run(client):
    t = client.get("/node/demo/pkg.m.load_orders?at=2").text
    assert 'data-at-run="2"' in t


def test_graph_page_resolves_a_reason_id_to_its_run_and_says_so(client):
    rid = _reason_ids(client._db)[0]
    r = client.get(f"/graph/demo?at=reason:{rid}&scope=full")
    assert r.status_code == 200
    t = r.text
    assert f'data-at-reason="{rid}"' in t and 'data-at-run="2"' in t
    assert "record" in t                                                  # the title explains the hop, never silently


def test_the_switch_bar_keeps_the_typed_at_across_the_three_views(client):
    rid = _reason_ids(client._db)[0]
    t = client.get(f"/node/demo/pkg.m.load_orders?at=reason:{rid}").text
    m = re.search(r'data-view-bar data-view="(\w+)"\s+data-project="([^"]*)" data-node="([^"]*)" data-at="([^"]*)"', t)
    assert m and m.group(4) == f"reason:{rid}"
    links = dict((v, _html.unescape(h)) for h, v in re.findall(r'<a href="([^"]+)" data-view-link="(\w+)"', t))
    assert links["graph"].endswith(f"at=reason%3A{rid}")
    assert links["node"].endswith(f"at=reason%3A{rid}")
