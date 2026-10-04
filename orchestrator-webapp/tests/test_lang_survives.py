"""`?lang=zh` must survive the 2 s poll and the view bar.

The full page rendered Chinese, then the first `/api/dashboard` poll (whose URL
dropped `lang`) swapped the English partial over it; the view-bar links and the
bar's search form dropped `lang` too, so one click went back to English. And the
poll's ETag ignored `lang`, so a cached English fragment could answer a Chinese
poll with 304. These tests pin all three.
"""
from __future__ import annotations

import html
import importlib
import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "orchestrator-webapp" / "tests"))

from test_routes import _seed_db, _seed_state_graph, _seed_session  # noqa: E402


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
    c = TestClient(main.app); c._seeded = seeded
    return c


def _poll_url(page: str) -> str:
    m = re.search(r'id="dashboard-content"\s+hx-get="([^"]*)"', page)
    assert m, "no polling div on the page"
    return html.unescape(m.group(1))


def _bar_links(page: str) -> dict[str, str]:
    return {m.group(2): html.unescape(m.group(1))
            for m in re.finditer(r'<a href="([^"]*)" data-view-link="(\w+)"', page)}


# ── the poll keeps the language ──────────────────────────────────────────────

def test_the_poll_url_carries_lang_on_the_home_page(client):
    assert "lang=zh" in _poll_url(client.get("/?lang=zh").text)


def test_the_poll_url_carries_lang_and_the_plan_on_a_plan_page(client):
    pid = client._seeded["plan_id"]
    url = _poll_url(client.get(f"/plan/{pid}?node=pkg.m.load_orders&lang=zh").text)
    assert "lang=zh" in url and f"plan={pid}" in url and "node=pkg.m.load_orders" in url


def test_the_poll_url_has_no_lang_by_default(client):
    assert "lang=" not in _poll_url(client.get("/").text)


def test_the_polled_partial_follows_the_poll_url(client):
    from app import vocab
    zh_word = vocab.ui("prior_decisions", "zh")
    url = _poll_url(client.get("/?lang=zh").text)
    assert zh_word in client.get(url).text


def test_the_etag_differs_per_language(client):
    en = client.get("/api/dashboard").headers["ETag"]
    zh = client.get("/api/dashboard?lang=zh").headers["ETag"]
    assert en != zh


def test_an_english_etag_does_not_304_a_chinese_poll(client):
    from app import vocab
    en = client.get("/api/dashboard").headers["ETag"]
    r = client.get("/api/dashboard?lang=zh", headers={"If-None-Match": en})
    assert r.status_code == 200
    assert vocab.ui("prior_decisions", "zh") in r.text


def test_a_chinese_poll_still_304s_against_its_own_etag(client):
    zh = client.get("/api/dashboard?lang=zh").headers["ETag"]
    r = client.get("/api/dashboard?lang=zh", headers={"If-None-Match": zh})
    assert r.status_code == 304


# ── the view bar keeps the language ──────────────────────────────────────────

@pytest.mark.parametrize("path", ["/node/demo/pkg.m.load_orders", "/graph/demo", "/outcomes?project=demo",
                                  "/search?q=fiscal&project=demo", "/ledger?project=demo", "/history",
                                  "/session/sess-A", "/"])
def test_every_view_bar_link_carries_lang(client, path):
    sep = "&" if "?" in path else "?"
    links = _bar_links(client.get(f"{path}{sep}lang=zh").text)
    assert links, f"{path}: no view-bar links rendered"
    for view, href in links.items():
        assert "lang=zh" in href, f"{path}: the {view} link dropped lang: {href}"


@pytest.mark.parametrize("path", ["/node/demo/pkg.m.load_orders", "/graph/demo", "/history", "/"])
def test_view_bar_links_have_no_lang_by_default(client, path):
    for view, href in _bar_links(client.get(path).text).items():
        assert "lang=" not in href, f"{path}: the {view} link grew a lang: {href}"


def test_the_bar_search_form_carries_lang(client):
    page = client.get("/node/demo/pkg.m.load_orders?lang=zh").text
    form = re.search(r'<form method="get" action="/search".*?</form>', page, flags=re.S).group(0)
    assert '<input type="hidden" name="lang" value="zh">' in form
    default = client.get("/node/demo/pkg.m.load_orders").text
    form = re.search(r'<form method="get" action="/search".*?</form>', default, flags=re.S).group(0)
    assert 'name="lang"' not in form


def test_keep_lang_appends_only_a_non_default_language():
    from app import queries
    assert queries.keep_lang("/graph/demo", "en") == "/graph/demo"
    assert queries.keep_lang("/graph/demo", None) == "/graph/demo"
    assert queries.keep_lang("/graph/demo", "zh") == "/graph/demo?lang=zh"
    assert queries.keep_lang("/node/d/x?at=run%3A2", "zh") == "/node/d/x?at=run%3A2&lang=zh"
    assert queries.keep_lang("/plan/p#step", "zh") == "/plan/p?lang=zh#step"
    assert queries.keep_lang("/x?lang=zh", "zh") == "/x?lang=zh"
    assert queries.keep_lang(None, "zh") is None
