"""DP phase 2d (Task 3c): the thing the product is FOR goes at the top.

provLedger's claim is "a past decision changed this plan". Until now you could
only find that by scrolling to a stats line on a node page. So the Task page's
first block is 因历史而变的决定 — every record this plan adopted, quoted, with a
link back to the task and the words that caused it; every node gets a trace
strip of its last significant moments; and a `?at=reason:<id>` marks the exact
span of the user's words the record cites, rather than making you find it.

The empty case is the one that matters: a plan that adopted nothing says so.
"本 plan 未采用任何历史记录" is information — a hidden block is not.
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

from orchestrator import db as odb  # noqa: E402
from test_routes import _seed_db, _seed_state_graph  # noqa: E402

VERBATIM = "上游说 v2 之后没有 discount 列了，别再依赖它"
PRE = "我们先把口径定下来："


@pytest.fixture
def client(tmp_path, monkeypatch):
    dbp = tmp_path / "orch.db"
    seeded = _seed_db(dbp)
    monkeypatch.setenv("ORCH_DB", str(dbp))
    _, reg = _seed_state_graph(tmp_path)
    monkeypatch.setenv("PSG_REGISTRY_PATH", str(reg))
    from app import queries, main
    importlib.reload(queries); importlib.reload(main)
    from fastapi.testclient import TestClient
    c = TestClient(main.app); c._seeded = seeded; c._db = dbp
    return c


def _adopt(dbp, plan_id, step_id, project="demo"):
    """An earlier plan recorded the user's words; THIS plan adopted them."""
    from orchestrator import provenance as pv
    conn = odb.open_db(dbp)
    conn.execute("INSERT INTO Plans (plan_id, original_goal, status, project, project_source, user_query, created_at) "
                 "VALUES ('EARLIER', '下线 discount 列', 'COMPLETED', ?, 'declared', ?, '2026-09-10 09:00:00')",
                 (project, PRE + VERBATIM))
    u = pv.insert_utterance(conn, session_id="s", project=project, plan_id="EARLIER",
                            text=PRE + VERBATIM, occurred_at="2026-09-10 09:00:00")
    rid = pv.insert_reason(conn, project=project, plan_id="EARLIER", node_key="nk_a", kind="technical",
                           run_id=2, step_id="EARLIER-B",
                           verbatim=(u, len(PRE), len(PRE) + len(VERBATIM)), recorded_by="agent")
    conn.execute("UPDATE Plans SET project = ? WHERE plan_id = ?", (project, plan_id))
    conn.execute("INSERT INTO read_hit (reason_id, project, plan_id, step_id, moment, at) "
                 "VALUES (?, ?, ?, ?, 'plan', '2026-09-16 10:00:00')", (rid, project, plan_id, step_id))
    conn.execute("INSERT INTO influence (reason_id, project, plan_id, step_id, node_key, via, by, at) "
                 "VALUES (?, ?, ?, ?, 'nk_a', 'headline_response', 'agent', '2026-09-16 10:01:00')",
                 (rid, project, plan_id, step_id))
    conn.commit(); conn.close()
    return rid


# ── the block ────────────────────────────────────────────────────────────────

def test_changed_by_history_returns_the_record_with_where_it_came_from(client):
    from app import queries
    pid = client._seeded["plan_id"]; step = client._seeded["step_ids"][0]
    rid = _adopt(client._db, pid, step)
    conn = queries.open_db_readonly(client._db)
    try:
        rows = queries.changed_by_history(conn, pid)
    finally:
        conn.close()
    assert len(rows) == 1
    r = rows[0]
    assert r["reason_id"] == rid and VERBATIM in r["text"] and r["tier"] == "stated"
    assert r["recorded_in_plan"] == "EARLIER" and r["recorded_in_step"] == "EARLIER-B"
    assert r["node_key"] == "nk_a" and r["by"] == "agent" and r["via"] == "headline_response"
    assert r["source_level"]


def test_the_block_comes_before_the_headline_on_the_task_page(client):
    pid = client._seeded["plan_id"]; step = client._seeded["step_ids"][0]
    _adopt(client._db, pid, step)
    t = client.get(f"/plan/{pid}").text
    assert 'data-panel="changed-by-history"' in t
    if 'data-panel="headline"' in t:
        assert t.index('data-panel="changed-by-history"') < t.index('data-panel="headline"')


def test_the_block_quotes_the_words_and_links_back_to_the_step(client):
    pid = client._seeded["plan_id"]; step = client._seeded["step_ids"][0]
    rid = _adopt(client._db, pid, step)
    t = client.get(f"/plan/{pid}").text
    assert VERBATIM in t
    assert f'href="/plan/EARLIER?node=pkg.m.load_orders&at=reason:{rid}#step-EARLIER-B"' in t
    assert 'data-verbatim="1"' in t          # the user's words are labelled as theirs


def test_a_plan_that_adopted_nothing_says_so(client):
    pid = client._seeded["plan_id"]
    t = client.get(f"/plan/{pid}").text
    assert 'data-panel="changed-by-history"' in t
    assert "本 plan 未采用任何历史记录" in t


def test_shown_and_adopted_stay_two_different_numbers_in_the_same_block(client):
    pid = client._seeded["plan_id"]; step = client._seeded["step_ids"][0]
    _adopt(client._db, pid, step)
    t = client.get(f"/plan/{pid}").text
    m = re.search(r'data-panel="changed-by-history"[^>]*data-shown="(\d+)" data-adopted="(\d+)"', t)
    assert m, "the block does not carry both counts"
    assert m.group(1) == "1" and m.group(2) == "1"


# ── the trace strip ──────────────────────────────────────────────────────────

def test_trace_strip_is_newest_first_and_never_longer_than_eight():
    from app import queries
    rows = [{"kind": "event", "at": f"2026-09-{d:02d}", "significant": True, "event_type": "node_changed",
             "tier": "observed", "run_id": d} for d in range(1, 21)]
    strip = queries.trace_strip({"timeline": rows}, limit=8)
    assert len(strip) == 8
    assert [r["at"] for r in strip] == sorted([r["at"] for r in strip], reverse=True)
    assert strip[0]["at"] == "2026-09-20"


def test_the_node_page_shows_the_strip_above_the_timeline(client):
    t = client.get("/node/demo/pkg.m.load_orders").text
    assert 'data-panel="trace-strip"' in t
    assert t.index('data-panel="trace-strip"') < t.index('data-timeline')


# ── landing on the words ─────────────────────────────────────────────────────

def test_at_reason_marks_exactly_the_cited_span(client):
    pid = client._seeded["plan_id"]; step = client._seeded["step_ids"][0]
    rid = _adopt(client._db, pid, step)
    t = client.get(f"/plan/EARLIER?at=reason:{rid}").text
    assert f"<mark" in t and VERBATIM in t
    marked = re.search(r"<mark[^>]*>(.*?)</mark>", t, re.S)
    assert marked and marked.group(1).strip() == VERBATIM, "the mark does not cover exactly the cited span"
    assert PRE in t and f"<mark" not in t.split(PRE)[0][-40:]    # the lead-in is not marked


def test_a_plan_page_without_at_marks_nothing(client):
    t = client.get("/plan/EARLIER").text if client.get("/plan/EARLIER").status_code == 200 else ""
    pid = client._seeded["plan_id"]; step = client._seeded["step_ids"][0]
    _adopt(client._db, pid, step)
    t = client.get("/plan/EARLIER").text
    assert "<mark" not in t


def test_mark_span_is_safe_when_the_span_is_nonsense():
    from app import queries
    assert queries.mark_span("abc", None, None) == "abc"
    assert queries.mark_span("abc", 5, 99) == "abc"            # out of range → untouched
    assert "<mark" in queries.mark_span("abcdef", 1, 3)
    assert "&lt;b&gt;" in queries.mark_span("<b>x</b>", 0, 3)  # the text is escaped, the mark is ours


# ── search ───────────────────────────────────────────────────────────────────

def test_search_is_read_only_and_groups_by_node(client):
    pid = client._seeded["plan_id"]; step = client._seeded["step_ids"][0]
    _adopt(client._db, pid, step)
    r = client.get("/search?q=discount&project=demo")
    assert r.status_code == 200
    t = r.text
    assert 'data-panel="search"' in t and "nk_a" in t or "pkg.m.load_orders" in t
    assert "回到当时的任务" in t


def test_search_says_when_it_could_not_use_the_index(client):
    """The dashboard's connection is read-only, so the FTS index cannot be built
    here. That is a real degradation and the page states it."""
    r = client.get("/search?q=discount&project=demo")
    assert "data-degraded" in r.text


def test_an_empty_search_is_a_sentence_not_a_blank_page(client):
    assert "输入要找的词" in client.get("/search").text
    r = client.get("/search?q=zzzznotathing&project=demo")
    assert r.status_code == 200 and "没有找到" in r.text


def test_search_did_not_add_a_write_route(client):
    from app import main
    verbs = set()
    for route in main.app.routes:
        verbs |= {m for m in getattr(route, "methods", set()) if m not in ("HEAD", "OPTIONS")}
    assert verbs == {"GET"}
