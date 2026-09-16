"""DP phase 2d (Task 3d): the ledger keeps its terms, people read theirs.

`headline`, `read_hit`, `influence`, `evidence_level`, `tier=asserted` are
precise and they are the schema's words, not a reader's. Renaming the columns
would be worse — the whole point is that the distinctions survive — so the
translation lives in one table and only the human-facing TEXT goes through it.

Two hard rules this suite enforces:

  · every enum value the ledger can produce has an entry, in both languages.
    A missing word must FAIL here, not degrade into an English token sitting in
    a Chinese sentence where nobody notices it.
  · the machine attributes (data-tier, data-severity, data-role) keep their
    ORIGINAL tokens. ETags, the rest of this suite, and anyone scraping the page
    must not be able to tell that the wording changed.
"""
from __future__ import annotations

import importlib
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

TIERS = ("observed", "derived", "asserted", "stated", "unstated")
SEVERITIES = ("blocking", "warning", "info")
LEVELS = ("linked", "verbal", "task_context", "unstated")
ROLES = ("reason", "constraint", "rejected_path")
EVENTS = ("node_added", "node_changed", "node_renamed", "node_moved", "column_dropped",
          "node_removed", "identity_asserted", "node_matched", "identity_kept")
MOMENTS = ("plan", "edit", "close", "why")
VIAS = ("headline_response", "reason_because", "constraint_ack")
STATUSES = ("PENDING", "IN_PROGRESS", "COMPLETED", "FAILED", "NEEDS_REVIEW")

ALL = {"tier": TIERS, "severity": SEVERITIES, "evidence_level": LEVELS, "role": ROLES,
       "event": EVENTS, "moment": MOMENTS, "via": VIAS, "status": STATUSES}


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


# ── the table is complete ────────────────────────────────────────────────────

@pytest.mark.parametrize("kind,values", sorted(ALL.items()))
@pytest.mark.parametrize("lang", ["zh", "en"])
def test_every_enum_value_has_a_word_in_both_languages(kind, values, lang):
    from app import vocab
    for v in values:
        word = vocab.say(v, kind=kind, lang=lang)
        assert word and word != v, f"{kind}.{v} has no {lang} word"
        assert not vocab.is_fallback(v, kind=kind, lang=lang), f"{kind}.{v} fell back"


def test_an_unknown_word_is_echoed_and_flagged_rather_than_guessed():
    from app import vocab
    assert vocab.say("no_such_tier", kind="tier") == "no_such_tier"
    assert vocab.is_fallback("no_such_tier", kind="tier") is True
    assert vocab.say(None, kind="tier") == ""


def test_the_five_tiers_still_read_as_five_different_things():
    from app import vocab
    words = {vocab.say(t, kind="tier") for t in TIERS}
    assert len(words) == 5, "two tiers translate to the same phrase — the distinction is gone"
    assert vocab.say("stated", kind="tier") == "你的原话"
    assert vocab.say("unstated", kind="tier") == "未说明"


def test_the_quiet_events_say_they_are_folded():
    from app import vocab
    assert vocab.say("node_matched", kind="event") == vocab.say("identity_kept", kind="event") == "无变化"


def test_counts_read_as_sentences():
    from app import vocab
    assert vocab.shown(3) == "被看到 3 次" and vocab.shown(0) == "没有被看到过"
    assert vocab.adopted(2) == "改变了 2 次计划" and vocab.adopted(0) == "没有改变过任何计划"
    assert "《任务标题》" in vocab.adopted_by("任务标题")


# ── the filter and the switch ────────────────────────────────────────────────

def test_the_say_filter_is_available_to_every_template(client):
    from app import main
    assert "say" in main.TEMPLATES.env.filters


def test_lang_en_switches_the_words(client):
    zh = client.get("/graph/demo").text
    en = client.get("/graph/demo?lang=en").text
    assert "开工前的提醒" in zh or "记录" in zh
    assert "开工前的提醒" not in en


# ── the machine attributes are untouched ─────────────────────────────────────

def test_data_attributes_keep_the_ledgers_own_tokens(client):
    pid = client._seeded["plan_id"]
    for url in ("/", f"/plan/{pid}", "/graph/demo", "/node/demo/pkg.m.load_orders"):
        t = client.get(url).text
        for m in re.finditer(r'data-(tier|severity|role)="([^"]*)"', t):
            kind, value = m.group(1), m.group(2)
            assert value in ALL[kind if kind != "role" else "role"] or value == "", \
                f"{url} put a translated word in data-{kind}: {value!r}"


def test_the_polled_partial_is_byte_identical_for_the_machine_parts(client):
    """The 2s ETag poll must not start changing just because the wording did."""
    a = client.get("/api/dashboard")
    b = client.get("/api/dashboard")
    assert a.headers.get("ETag") == b.headers.get("ETag")


# ── no internal token leaks into the prose ───────────────────────────────────

def _prose(html: str) -> str:
    """The page with every attribute value and <script>/<style> body removed —
    what a reader actually sees."""
    html = re.sub(r"<script.*?</script>", " ", html, flags=re.S)
    html = re.sub(r"<style.*?</style>", " ", html, flags=re.S)
    html = re.sub(r"<[^>]*>", " ", html)
    return html


@pytest.mark.parametrize("path", ["/graph/demo", "/node/demo/pkg.m.load_orders"])
def test_no_node_key_is_shown_as_prose(client, path):
    assert "nk_" not in _prose(client.get(path).text)


@pytest.mark.parametrize("path", ["/graph/demo", "/node/demo/pkg.m.load_orders"])
def test_no_bare_enum_token_is_shown_as_prose(client, path):
    prose = _prose(client.get(path).text)
    leaked = [w for w in ("observed", "asserted", "unstated", "read_hit", "influence",
                          "headline_response", "evidence_level", "rejected_path") if w in prose]
    assert leaked == [], f"{path} shows internal words to the reader: {leaked}"
