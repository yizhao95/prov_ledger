"""Every link goes somewhere, and every abbreviation can be expanded (DP 2d).

Two failure modes this suite exists for:

  · a link that 404s or drops the context triple. The dashboard's whole value is
    that you can follow a record back to the task and the words behind it; a
    broken hop is worse than no link, because it looks like the trail ends.
  · a number or a label with no way to find out what it means. The page is terse
    on purpose now — terse is only acceptable if hovering explains it.
"""
from __future__ import annotations

import importlib
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

import pytest

REPO = Path(__file__).resolve().parents[2]
ORCH_BACKEND = REPO / "orchestrator-backend"
WEBAPP = REPO / "orchestrator-webapp"
sys.path.insert(0, str(WEBAPP))
sys.path.insert(0, str(ORCH_BACKEND))
sys.path.insert(0, str(REPO / "orchestrator-webapp" / "tests"))

from orchestrator import db as odb  # noqa: E402
from test_routes import _seed_db, _seed_state_graph, _seed_reasons_and_constraints, _seed_session  # noqa: E402


@pytest.fixture
def client(tmp_path, monkeypatch):
    dbp = tmp_path / "orch.db"
    seeded = _seed_db(dbp)
    monkeypatch.setenv("ORCH_DB", str(dbp))
    _, reg = _seed_state_graph(tmp_path)
    monkeypatch.setenv("PSG_REGISTRY_PATH", str(reg))
    _seed_reasons_and_constraints(dbp)
    _seed_session(dbp, seeded["plan_id"])
    from app import queries, main
    importlib.reload(queries); importlib.reload(main)
    from fastapi.testclient import TestClient
    c = TestClient(main.app); c._seeded = seeded; c._db = dbp
    return c


def _pages(client):
    pid = client._seeded["plan_id"]
    return ["/", f"/plan/{pid}", "/history", "/outcomes", "/graph/demo",
            "/node/demo/pkg.m.load_orders", "/session/sess-A", "/search?q=orders&project=demo"]


def _hrefs(html: str) -> list[str]:
    return [h for h in re.findall(r'<a [^>]*href="([^"]+)"', html)
            if not h.startswith(("http://", "https://", "mailto:", "#"))]


# ── every link resolves ──────────────────────────────────────────────────────

def test_every_link_on_every_view_resolves(client):
    seen, broken = set(), []
    for page in _pages(client):
        html = client.get(page).text
        for href in _hrefs(html):
            if href in seen:
                continue
            seen.add(href)
            r = client.get(href)
            if r.status_code != 200:
                broken.append((page, href, r.status_code))
    assert broken == [], f"broken links: {broken}"
    assert len(seen) >= 10, f"only {len(seen)} links crawled — the pages are not linked up"


def test_a_record_link_keeps_the_whole_triple_and_lands_on_the_step(client):
    html = client.get("/node/demo/pkg.m.load_orders").text
    backs = [h for h in _hrefs(html) if h.startswith("/plan/") and "node=" in h]
    assert backs, "no task link carries the node"
    for href in backs:
        assert "at=" in href, f"the anchor is lost: {href}"
        r = client.get(href)
        assert r.status_code == 200
        if "#step-" in href and 'id="step-' in r.text:
            # a plan that renders steps must render THE step the link names; a
            # record can also cite a step of a plan whose steps predate the
            # column, and an anchor that finds nothing just stays at the top
            step = href.split("#step-")[1]
            assert f'id="step-{step}"' in r.text or "REVIEW" in step, \
                f"the step anchor does not exist: {step}"


def test_no_link_exposes_an_internal_id_as_its_text(client):
    """The id belongs in title=/data-*, not in the sentence (layout spec 8)."""
    for page in _pages(client):
        html = client.get(page).text
        for text in re.findall(r"<a [^>]*>(.*?)</a>", html, re.S):
            body = re.sub(r"<[^>]*>", "", text).strip()
            assert not body.startswith("nk_"), f"{page} shows a node key as link text: {body}"


# ── every abbreviation can be expanded ───────────────────────────────────────

def test_the_tier_label_carries_its_definition(client):
    html = client.get("/node/demo/pkg.m.load_orders").text
    tiers = re.findall(r'<span[^>]*data-tier="(\w+)"[^>]*title="([^"]*)"', html)
    assert tiers, "no tier label carries a title"
    for tier, title in tiers:
        assert tier in title, f"the {tier} tooltip does not define it: {title!r}"


def test_the_hit_count_says_which_moments_it_came_from(client):
    html = client.get("/node/demo/pkg.m.load_orders").text
    stats = re.findall(r'data-stats="\d+"[^>]*title="([^"]*)"', html)
    assert stats, "the hit count has no tooltip"
    assert any("plan" in t and "edit" in t and "why" in t for t in stats), \
        f"the tooltip does not break the count down by moment: {stats[:2]}"


def test_the_graph_nodes_carry_what_they_are(client):
    html = client.get("/graph/demo?mode=full").text
    assert "n.node_type" in html and "file_path" in html and "records" in html
    assert "open node" in html


def test_a_finding_points_at_the_record_behind_it(client):
    from test_routes import _seed_headline
    pid = client._seeded["plan_id"]
    _seed_headline(client._db, pid, client._seeded["step_ids"][0])
    html = client.get("/api/dashboard").text
    titles = re.findall(r'data-finding="[^"]*"[^>]*title="([^"]*)"', html)
    assert titles, "a finding carries no tooltip"
    assert any(re.search(r"\d", t) for t in titles), f"no evidence id in the tooltip: {titles[:2]}"
