"""`GET /ledger` — ask the ledger, read-only (DP phase 2e, Task 4; spec §21, J6/J7).

The page is the same pipeline as `provledger ask`: code locates, the model may
only pick and restate, code checks. What this suite guards is the page's half —
that every conclusion is a link back to the record it rests on, that an absence
is marked as an absence, that the scope is on the page, and that the whole
surface is GET.
"""
from __future__ import annotations

import importlib
import json
import re

import pytest

# FL-006: nothing outside analyzer/_host.py splices the bundled backend path into
# sys.path, and the webapp reaches the backend only as `provledger`. Both are
# conftest.py's job (orchestrator-webapp/conftest.py) — a test that wires its own
# path is a test that proves something production does not do.
from test_routes import _seed_db, _seed_reasons_and_constraints, _seed_state_graph  # noqa: E402

QUESTION = "why must load_orders keep paid orders only?"


def _a_cite_from_the_fact_table(prompt: str) -> str:
    """A real id, taken from the fact table half of the prompt. The RULES half
    carries `[#3]` as an example, and a stub that grabs that one is testing the
    example, not the ledger."""
    table = prompt.split("## Fact table", 1)[-1]
    m = re.search(r"\[#(\d+)\] asserted", table) or re.search(r"\[#(\d+)\]", table)
    return m.group(1) if m else "1"


@pytest.fixture
def client(tmp_path, monkeypatch):
    dbp = tmp_path / "orch.db"
    seeded = _seed_db(dbp)
    monkeypatch.setenv("ORCH_DB", str(dbp))
    _, reg = _seed_state_graph(tmp_path)
    monkeypatch.setenv("PSG_REGISTRY_PATH", str(reg))
    _seed_reasons_and_constraints(dbp)
    from app import main, queries
    importlib.reload(queries); importlib.reload(main)
    from fastapi.testclient import TestClient
    c = TestClient(main.app); c._seeded = seeded; c._db = dbp
    return c


@pytest.fixture
def with_model(monkeypatch):
    """A stub runner in place of headless claude — the suite never calls a model."""
    from app import main

    def runner(prompt, *, model=None, timeout_s=None):
        if '"chosen"' in prompt:
            return json.dumps({"chosen": ["pkg.m.load_orders"], "basis": "the question names it"})
        cite = _a_cite_from_the_fact_table(prompt)
        # the JSON shape the prompt demands, behind the paragraph a host plugin
        # prepends to `result` on this machine — it must fall off, not be judged
        return "Memory capture is paused due to a quota cooldown.\n\n" + json.dumps({"sentences": [
            f"Finance reconciles on paid orders, so load_orders keeps only those [#{cite}].",
            "This sentence has no id and must not survive.",
            f"It removed 4127 rows last week [#{cite}]."]})
    monkeypatch.setattr(main, "_ask_runner", lambda request: (runner, "stub"))
    return runner


# ── J6: the whole surface is GET ─────────────────────────────────────────────

def test_j6_the_dashboard_exposes_no_non_get_route(client):
    from app import main
    bad = [(r.path, sorted(r.methods)) for r in main.app.routes
           if getattr(r, "methods", None) and not set(r.methods) <= {"GET", "HEAD"}]
    assert bad == [], f"a non-GET route exists: {bad}"
    ledger = sorted(r.path for r in main.app.routes if str(getattr(r, "path", "")).startswith("/ledger"))
    assert ledger == ["/ledger", "/ledger/card", "/ledger/results"]
    for path in ledger:
        assert client.post(path).status_code == 405


# ── the answer, its links, its absences, its scope ───────────────────────────

def test_every_conclusion_links_back_to_the_record_it_rests_on(client, with_model):
    t = client.get(f"/ledger?q={QUESTION}&project=demo").text
    sentences = re.findall(r'<li[^>]*data-sentence="(\d+)"[^>]*>(.*?)</li>', t, re.S)
    assert sentences, "no answer sentence rendered"
    assert "Finance reconciles on paid orders" in t
    cites = re.findall(r'<a [^>]*data-cite="(#\w+)"[^>]*href="([^"]+)"', t)
    assert cites, "a cite is printed but not linked"
    for cite, href in cites:
        assert "/node/demo/" in href or href.startswith("/plan/"), href
        if "/node/" in href:
            assert "at=reason:" in href or "at=run:" in href, f"the anchor is untyped: {href}"
        assert client.get(href).status_code == 200


def test_the_dropped_sentences_are_counted_on_the_page(client, with_model):
    t = client.get(f"/ledger?q={QUESTION}&project=demo").text
    assert "This sentence has no id" not in t and "4127" not in t
    dropped = re.search(r'data-dropped="(\d+)"', t)
    assert dropped and int(dropped.group(1)) == 2
    assert "uncited" in t and "number not in the fact table" in t


def test_an_absence_is_marked_as_an_absence(client, with_model):
    t = client.get(f"/ledger?q={QUESTION}&project=demo").text
    absences = re.findall(r'data-absence="(\w+)"', t)
    assert absences, "no absence sentence carries data-absence"
    assert "never_verified" in absences
    assert "[scope]" in t


def test_the_scope_line_is_on_the_page_and_says_what_was_searched_and_what_it_cost(client, with_model):
    t = client.get(f"/ledger?q={QUESTION}&project=demo").text
    scope = re.search(r'data-scope="[^"]*"[^>]*>(.*?)</', t, re.S)
    assert scope and scope.group(1).strip().startswith("Scope:")
    assert "candidate" in scope.group(1) and "chosen" in scope.group(1)
    # the page never gets slower in silence: the cost is on the line, and the
    # machine-readable number is beside it
    assert "Computed in" in scope.group(1) and scope.group(1).rstrip().endswith("s.")
    ms = re.search(r'data-elapsed-ms="(\d+)"', t)
    assert ms and int(ms.group(1)) >= 0


def test_open_records_expands_the_fact_table_and_export_card_is_a_get_link(client, with_model):
    t = client.get(f"/ledger?q={QUESTION}&project=demo").text
    assert 'data-records' in t and "Fact table" in t
    card = re.search(r'href="(/ledger/card\?ask_id=\d+)"', t)
    assert card, "no [Export card] link"
    r = client.get(card.group(1))
    assert r.status_code == 200 and r.text.startswith("# Evidence card")
    assert "Integrity at export time" in r.text and "text/markdown" in r.headers["content-type"]


def test_the_page_states_the_command_for_saying_the_answer_is_wrong(client, with_model):
    t = client.get(f"/ledger?q={QUESTION}&project=demo").text
    assert re.search(r"provledger ask feedback \d+ wrong", t), "the page must show the command, not a button"


# ── J7: no model, and the four other reasons there may be no summary ─────────

def test_j7_without_a_model_the_page_shows_the_fact_table_and_says_why(client):
    t = client.get(f"/ledger?q={QUESTION}&project=demo").text
    assert "summary unavailable: no model configured" in t
    assert "Fact table" in t and "Scope:" in t
    assert 'data-degraded="1"' in t and 'data-degraded-reason="no_model"' in t
    assert "PROVLEDGER_ASK_RUNNER=claude" in t, "the help line belongs on THIS reason"


@pytest.mark.parametrize("runner,reason,says", [
    (lambda p, *, model=None, timeout_s=None: ("", {"rc": 3, "stderr_head": "Invalid API key"}),
     "empty", "model returned nothing (rc 3; stderr: Invalid API key)"),
    (lambda p, *, model=None, timeout_s=None: (_ for _ in ()).throw(RuntimeError("claude not on PATH")),
     "failed", "model call failed: RuntimeError: claude not on PATH"),
    # the real one: the account's budget for that model ran out, `claude -p`
    # exited 1 and said so in good JSON. The sentence is the answer here.
    (lambda p, *, model=None, timeout_s=None: ("", {"rc": 1, "refused": True, "is_error": True, "model": "fable",
                                                    "result": "Your Fable limit is reached. Switch to another model."}),
     "refused", "model call refused [fable]: Your Fable limit is reached. Switch to another model. — "
                "try --model sonnet"),
])
def test_the_page_names_which_failure_it_was_not_just_no_model(client, monkeypatch, runner, reason, says):
    """Four different failures printed one sentence, and a packaging bug went out
    as "you have no model" for weeks. The page says which one happened."""
    from app import main
    monkeypatch.setattr(main, "_ask_runner", lambda request: (runner, "claude"))
    t = client.get(f"/ledger?q={QUESTION}&project=demo").text
    assert f'data-degraded-reason="{reason}"' in t
    assert says in t
    assert "PROVLEDGER_ASK_RUNNER=claude" not in t, "a model WAS configured; do not tell them to configure one"


def test_a_question_nothing_matches_says_there_were_no_candidates(client, with_model):
    t = client.get("/ledger?q=what+did+the+zzzqqq+widget+decide&project=demo").text
    assert 'data-degraded-reason="no_candidates"' in t
    assert "no candidate nodes matched the question" in t


def test_a_chinese_sentence_is_kept_and_flagged_not_deleted(client, monkeypatch):
    """The host's `~/.claude/settings.json` says `"language": "Chinese"`, so the
    headless model answered in Chinese. Deleting for that emptied a correct
    answer; language is a preference, citations are correctness."""
    from app import main

    def runner(prompt, *, model=None, timeout_s=None):
        if '"chosen"' in prompt:
            return json.dumps({"chosen": ["pkg.m.load_orders"], "basis": "the question names it"})
        cite = _a_cite_from_the_fact_table(prompt)
        return json.dumps({"sentences": [f"Finance reconciles on paid orders [#{cite}].",
                                         f"财务只核对已付款订单 [#{cite}]."]})

    monkeypatch.setattr(main, "_ask_runner", lambda request: (runner, "stub"))
    en = client.get(f"/ledger?q={QUESTION}&project=demo").text
    assert "财务只核对已付款订单" in en, "a correct sentence is not deleted for its language"
    assert 'data-language-mismatch="some"' in en and "--lang zh" in en
    assert 'data-drop=' not in en
    zh = client.get(f"/ledger?q={QUESTION}&project=demo&lang=zh").text
    assert "财务只核对已付款订单" in zh and "Finance reconciles on paid orders" in zh
    assert "--lang en" in zh, "asked in Chinese, the English half is the odd one — and it is still kept"


def test_a_plugin_preamble_never_becomes_a_sentence_the_model_is_blamed_for(client, with_model):
    """claude-mem prepends its own paragraph to every `result`. Read as prose it
    counted as an uncited sentence the model had invented. It never was."""
    t = client.get(f"/ledger?q={QUESTION}&project=demo").text
    assert "Memory capture is paused" not in t
    assert re.search(r'data-dropped="(\d+)"', t).group(1) == "2", "the two bad sentences, not the plugin's"


def test_the_page_logs_why_the_model_said_nothing(client, monkeypatch):
    from app import main
    from provledger import ask as ask_mod, db as odb
    monkeypatch.setattr(main, "_ask_runner",
                        lambda request: ((lambda p, *, model=None, timeout_s=None: ("", {"rc": 3, "stderr_head": "boom"})),
                                         "claude"))
    t = client.get(f"/ledger?q={QUESTION}&project=demo").text
    ask_id = int(re.search(r'data-ask-id="(\d+)"', t).group(1))
    conn = odb.open_db(client._db)
    try:
        row = ask_mod.get_ask(conn, ask_id)
    finally:
        conn.close()
    assert row["runner_detail"]["summarize"]["outcome"] == "empty"
    assert row["runner_detail"]["summarize"]["detail"]["rc"] == 3
    assert row["elapsed_ms"] is not None


# ── the chrome: an entry in the switch bar, and the words ────────────────────

def test_the_switch_bar_carries_a_ledger_entry(client):
    for page in ("/graph/demo", "/node/demo/pkg.m.load_orders", f"/ledger?project=demo"):
        t = client.get(page).text
        assert 'data-view-link="ledger"' in t, f"{page} has no Ledger entry"
    t = client.get("/ledger?project=demo").text
    assert 'data-view-bar' in t and 'data-view="ledger"' in t


def test_lang_zh_switches_the_words_and_keeps_the_tokens(client, with_model):
    en = client.get(f"/ledger?q={QUESTION}&project=demo").text
    zh = client.get(f"/ledger?q={QUESTION}&project=demo&lang=zh").text
    assert "Scope:" in en and "检索范围：" in zh
    assert 'data-absence="never_verified"' in zh, "a machine attribute keeps its token in both languages"
    assert "问一句" in zh or "账本" in zh


def test_the_partial_is_the_same_result_without_the_chrome(client, with_model):
    full = client.get(f"/ledger?q={QUESTION}&project=demo").text
    part = client.get(f"/ledger/results?q={QUESTION}&project=demo").text
    assert "<html" not in part and 'id="ledger-results"' not in part
    assert "Finance reconciles on paid orders" in part
    assert re.search(r'data-scope="[^"]*"', part)
    assert 'hx-get="/ledger/results"' in full, "the form loads the partial"


def test_an_empty_question_asks_for_one_and_writes_nothing(client):
    from provledger import db as odb
    t = client.get("/ledger?project=demo").text
    assert "Scope:" not in t and "summary unavailable" not in t
    conn = odb.open_db(client._db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM ask_log").fetchone()[0] == 0
    finally:
        conn.close()
