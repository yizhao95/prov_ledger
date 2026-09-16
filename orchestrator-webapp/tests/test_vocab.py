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
        assert word, f"{kind}.{v} has no {lang} word"
        # `unstated` is the agreed English word for itself — completeness is what
        # this test is for, so it asks the table, not the string
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
    assert vocab.say("stated", kind="tier") == "stated"
    assert vocab.say("unstated", kind="tier") == "unstated"
    assert vocab.say("stated", kind="tier", lang="zh") == "用户陈述"


def test_the_quiet_events_say_they_are_folded():
    from app import vocab
    assert vocab.say("node_matched", kind="event") == vocab.say("identity_kept", kind="event") == "unchanged"
    assert vocab.say("node_matched", kind="event", lang="zh") == "无变更"


def test_counts_read_as_measurements_not_chatter():
    """Professional register: a count states the measurement, it does not narrate."""
    from app import vocab
    assert vocab.shown(3) == "Surfaced 3" and vocab.shown(0) == "Surfaced 0"
    assert vocab.adopted(2) == "Adopted 2" and vocab.adopted(0) == "Adopted 0"
    assert vocab.adopted_by("Drop the discount column") == "Adopted by plan Drop the discount column"
    assert vocab.folded(16) == "Unchanged matches: 16"
    assert vocab.shown(3, "zh") == "呈现 3 次" and vocab.adopted(2, "zh") == "被 2 个计划采纳"
    assert vocab.adopted_by("下线 discount 列", "zh") == "被计划《下线 discount 列》采纳"


# ── the filter and the switch ────────────────────────────────────────────────

def test_the_say_filter_is_available_to_every_template(client):
    from app import main
    assert "say" in main.TEMPLATES.env.filters


def test_english_is_the_default_and_zh_is_the_switch(client):
    """The package is used in English; Chinese is available, not assumed."""
    from app import vocab
    assert vocab.DEFAULT_LANG == "en"
    default = client.get("/node/demo/pkg.m.load_orders").text
    zh = client.get("/node/demo/pkg.m.load_orders?lang=zh").text
    assert "Change history" in default and "变更历史" not in default
    assert "变更历史" in zh


def test_the_agreed_english_register(client):
    from app import vocab
    # the ledger's own terms, used directly — and each label carries a one-line
    # definition in its tooltip, because a label alone teaches nobody
    for t in TIERS:
        assert vocab.say(t, kind="tier") == t
        assert vocab.define(t, kind="tier").startswith(t + ":")
    for lv in LEVELS:
        assert vocab.define(lv, kind="evidence_level")
    assert vocab.say("task_context", kind="evidence_level") == "task-context"
    assert vocab.say("blocking", kind="severity") == "Blocking"
    assert vocab.say("warning", kind="severity") == "Warning"
    assert vocab.say("info", kind="severity") == "Info"
    assert vocab.say("constraint", kind="role") == "constraint"
    assert vocab.say("rejected_path", kind="role") == "rejected alternative"
    assert vocab.say("reason", kind="role") == "reason"
    assert vocab.say("headline", kind="term") == "Findings"
    assert vocab.say("influence", kind="term") == "Decisions relied on"


def test_the_agreed_chinese_register(client):
    from app import vocab
    z = lambda v, k: vocab.say(v, kind=k, lang="zh")
    assert z("observed", "tier") == "系统观测" and z("derived", "tier") == "系统推导"
    assert z("asserted", "tier") == "模型断言" and z("unstated", "tier") == "未陈述"
    assert z("blocking", "severity") == "需响应" and z("warning", "severity") == "需关注" and z("info", "severity") == "参考"
    assert z("linked", "evidence_level") == "可核对链接" and z("verbal", "evidence_level") == "原话记录"
    assert z("task_context", "evidence_level") == "仅任务上下文"
    assert z("constraint", "role") == "生效约束" and z("rejected_path", "role") == "已否决方案"
    assert z("reason", "role") == "变更原因"
    assert z("headline", "term") == "计划前置检查"
    assert z("influence", "term") == "依据的历史记录"


def test_every_ui_phrase_exists_in_both_columns():
    """The page strings live in the same table as the enums, so a phrase cannot
    be added in one language and silently left English in the other."""
    from app import vocab
    assert vocab.UI, "no UI phrase table"
    for key, row in vocab.UI.items():
        for lang in LANGS:
            assert row.get(lang), f"UI.{key} has no {lang} phrase"
    for key in ("prior_decisions", "prior_decisions_empty", "active_constraints", "change_summary",
                "change_history", "dependencies", "node_ledger", "read_only", "home", "history",
                "skills_activated", "original_instruction", "context_estimate", "reduced_view",
                "expand_full", "unchanged_matches", "no_match", "search_placeholder"):
        assert key in vocab.UI, f"UI table is missing {key}"


LANGS = ("en", "zh")


COLLOQUIAL = ("因历史而变的决定", "还管着它的规矩", "走不通的路", "开工前的提醒", "必须回应",
              "值得注意", "仅供参考", "被看到", "这条记录改变了", "最近发生了什么",
              "一路怎么变的", "看它连着谁", "这个东西一路怎么变的", "系统观测到", "系统推出",
              "agent 的判断", "你的原话", "有链接可查", "只有任务脉络", "无变化的匹配")

LEFTOVER_ENGLISH = ("Back to Home", "Back to History", "Skills Activated",
                    "Original Query", "verbatim user prompt", "approx_tokens", "plan headline")


@pytest.mark.parametrize("lang", ["", "?lang=zh"])
def test_no_colloquial_phrase_survives_in_either_language(client, lang):
    for path in ("/graph/demo", "/node/demo/pkg.m.load_orders"):
        prose = _prose(client.get(path + lang).text)
        found = [w for w in COLLOQUIAL if w in prose]
        assert found == [], f"{path}{lang} still reads colloquially: {found}"


def test_the_leftover_english_chrome_is_gone(client):
    pid = client._seeded["plan_id"]
    for path in ("/", f"/plan/{pid}", "/history", "/graph/demo", "/node/demo/pkg.m.load_orders"):
        prose = _prose(client.get(path).text)
        found = [w for w in LEFTOVER_ENGLISH if w in prose]
        assert found == [], f"{path} still shows raw chrome: {found}"


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
    """Internal tokens only. `unstated` and `observed` are now parts of agreed
    phrases (`unstated`, `system-observed`), so the match is on word boundaries
    and the list is the words that are ONLY ever schema identifiers."""
    prose = _prose(client.get(path).text)
    leaked = [w for w in ("read_hit", "influence", "headline_response", "evidence_level",
                          "rejected_path", "node_matched", "identity_kept", "change_reason")
              if re.search(rf"\b{w}\b", prose)]
    assert leaked == [], f"{path} shows internal words to the reader: {leaked}"
