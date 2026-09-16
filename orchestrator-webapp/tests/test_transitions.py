"""DP phase 2d (Task 3): switching views keeps the anchor and glides.

The three views already share one context triple; what they did not share was
continuity — every switch was a full reload that threw the page away. hx-boost
turns navigation into a swap, and the View Transitions API animates the swap
where the browser has it.

Two things this suite exists to stop:

  · the 2s ETag poll of /api/dashboard animating. A refresh is not a navigation;
    a dashboard that fades every two seconds is a dashboard nobody can read.
  · the animation becoming mandatory. `prefers-reduced-motion` must switch it
    off, and a browser without `document.startViewTransition` must still swap.
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
sys.path.insert(0, str(REPO / "orchestrator-webapp" / "tests"))

from test_routes import _seed_db, _seed_state_graph, _seed_session  # noqa: E402

BASE_HTML = WEBAPP / "app" / "templates" / "base.html"
TOKENS = json.loads((WEBAPP / "app" / "static" / "tokens.json").read_text(encoding="utf-8"))


@pytest.fixture
def client(tmp_path, monkeypatch):
    dbp = tmp_path / "orch.db"
    seeded = _seed_db(dbp)
    monkeypatch.setenv("ORCH_DB", str(dbp))
    _, reg = _seed_state_graph(tmp_path)
    monkeypatch.setenv("PSG_REGISTRY_PATH", str(reg))
    _seed_session(dbp, seeded["plan_id"])
    from app import queries, main
    importlib.reload(queries); importlib.reload(main)
    from fastapi.testclient import TestClient
    c = TestClient(main.app); c._seeded = seeded; c._db = dbp
    return c


def _pages(client):
    pid = client._seeded["plan_id"]
    return {
        "dashboard": "/",
        "plan": f"/plan/{pid}",
        "graph": "/graph/demo",
        "node": "/node/demo/pkg.m.load_orders",
        "outcomes": "/outcomes",
        "session": "/session/sess-A",
        "history": "/history",
    }


# ── every full page is boosted and named ─────────────────────────────────────

def test_every_full_page_is_boosted_and_names_its_main_region(client):
    for label, url in _pages(client).items():
        r = client.get(url)
        assert r.status_code == 200, f"{label} {url} -> {r.status_code}"
        t = r.text
        assert 'hx-boost="true"' in t, f"{label} is not boosted"
        assert 'data-transition="main"' in t, f"{label} has no named main region"


def test_the_switch_bar_is_named_separately_so_it_does_not_travel_with_the_body(client):
    t = client.get("/graph/demo").text
    assert 'data-transition="switcher"' in t
    css = BASE_HTML.read_text(encoding="utf-8")
    assert "view-transition-name: main" in css and "view-transition-name: switcher" in css


# ── the 2s poll must not animate ─────────────────────────────────────────────

def test_the_polled_partial_carries_no_transition_markers(client):
    r = client.get("/api/dashboard")
    assert r.status_code == 200
    t = r.text
    assert "hx-boost" not in t
    assert "view-transition-name" not in t and 'data-transition=' not in t


def test_the_wrapper_only_fires_for_boosted_navigation(client):
    """htmx:beforeSwap fires for the poll too — the handler has to ask whether
    this swap is a navigation before it starts a transition."""
    js = BASE_HTML.read_text(encoding="utf-8")
    assert "htmx:beforeSwap" in js
    assert "boosted" in js, "the transition wrapper does not check detail.boosted"


# ── never mandatory ──────────────────────────────────────────────────────────

def test_the_api_is_feature_detected_not_assumed(client):
    js = BASE_HTML.read_text(encoding="utf-8")
    assert "document.startViewTransition" in js
    assert re.search(r"(if\s*\(\s*!?\s*document\.startViewTransition|typeof document\.startViewTransition)", js), \
        "startViewTransition is called without a feature check"


def _media_block(css: str, at_rule: str) -> str:
    """The body of an @media rule, brace-balanced — a regex with a fixed closing
    indent breaks the moment the block gains a nested rule, which is exactly
    what this task adds to it."""
    start = css.index(at_rule)
    open_at = css.index("{", start)
    depth, i = 0, open_at
    while i < len(css):
        if css[i] == "{":
            depth += 1
        elif css[i] == "}":
            depth -= 1
            if depth == 0:
                return css[open_at + 1:i]
        i += 1
    raise AssertionError(f"unbalanced braces after {at_rule}")


def test_reduced_motion_switches_the_animation_off(client):
    css = BASE_HTML.read_text(encoding="utf-8")
    body = _media_block(css, "@media (prefers-reduced-motion: reduce)")
    assert "::view-transition-old(main)" in body and "::view-transition-new(main)" in body, \
        "reduced motion does not disable the view transition"
    assert "animation: none" in body


def test_the_duration_comes_from_the_token_file(client):
    css = BASE_HTML.read_text(encoding="utf-8")
    ms = TOKENS["motion"]["transition_ms"]
    assert f"{ms}ms" in css, f"the {ms}ms duration in tokens.json is not what the stylesheet uses"
    assert "::view-transition-old(main)" in css and "::view-transition-new(main)" in css


# ── layout spec 1: one column ────────────────────────────────────────────────

def test_the_body_is_one_column_of_the_width_the_tokens_declare(client):
    css = BASE_HTML.read_text(encoding="utf-8")
    assert "--pl-column-max" in css
    assert TOKENS["layout"]["column_max"] == "880px"
    for url in ("/graph/demo", "/node/demo/pkg.m.load_orders", "/"):
        assert "max-width: var(--pl-column-max)" in client.get(url).text


# ── the dashboard is still read-only ─────────────────────────────────────────

def test_no_route_accepts_anything_but_get(client):
    from app import main
    verbs = set()
    for route in main.app.routes:
        verbs |= {m for m in getattr(route, "methods", set()) if m not in ("HEAD", "OPTIONS")}
    assert verbs == {"GET"}, f"a non-GET route appeared: {verbs}"
