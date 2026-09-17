"""orchestrator.external_trigger — the external-artifact judge (DP phase 5,
Task 0; spec §4, design doc §5.2 and D8; acceptance C3 and C4).

The deterministic rules R0–R6 answer for code. Nothing answers for the deck:
a number on slide 4 can change with no line of code and no line of data behind
it, and today that change leaves the ledger empty. This module is the second
path — a headless model, given the five paired examples verbatim, that answers
two questions in order: is this change odd, and is the reason already in what
the person said.

What the tests below pin is the shape of the answer, not the model's opinion:

  · the five pairs map to exactly three verdicts — silent, ask, auto (C3);
  · a reason that is already in the user's words becomes a `stated` reason
    with rule_id X1 and nobody is asked anything;
  · an answer that is not the strict JSON object is `silent` — never an ask,
    because D8's asymmetry says a needless question is the irreversible error;
  · a span the utterance does not contain records nothing and asks nothing;
  · every verdict, including the ones that ask nobody anything, is one
    trigger_log row on path 'external' (C4).

Every test injects a stub runner. No test here reaches a model.
"""
from __future__ import annotations

import json

import pytest

from orchestrator import db, external_trigger as et, provenance as pv, triggers

PROJECT = "demo"
PLAN = "P5"
DECK = "decks/q3.pptx"


# ── the material ─────────────────────────────────────────────────────────────

def _plan(conn):
    conn.execute("INSERT INTO Plans (plan_id, original_goal, status, project, project_source, created_at) VALUES "
                 "(?, 'revise the Q3 deck', 'IN_PROGRESS', ?, 'declared', '2026-09-17 09:00:00')", (PLAN, PROJECT))
    conn.commit()
    return PLAN


def _said(conn, text: str) -> int:
    return pv.insert_utterance(conn, session_id="s1", project=PROJECT, plan_id=PLAN, text=text,
                               occurred_at="2026-09-17 09:10:00", commit=True)


def _occurrence(conn, node_key: str, at: str, value: str, path: str = DECK) -> int:
    from orchestrator.artifacts import anchor
    file_id = anchor.register_file(conn, PROJECT, path, "sha-" + path, "pptx", commit=False)
    row = {"project": PROJECT, "node_key": node_key, "file_id": file_id,
           "locator_json": json.dumps({"at": at, "kind": "pptx", "slide": int(at.split()[-1]), "shape": 2}, sort_keys=True),
           "value_text": value, "value_num": None, "seen_at": "2026-09-16 12:00:00",
           "tier": "observed", "by": "human", "recorded_at": "2026-09-16 12:00:00"}
    rid = pv._insert_chained(conn, "occurrence", row)
    conn.commit()
    return rid


@pytest.fixture
def ledger(conn):
    """A plan of project demo, two anchored external nodes, no graph at all —
    a deck's numbers live in the orchestrator database, not in a snapshot."""
    _plan(conn)
    _occurrence(conn, "metric:q3_conv", "slide 4", "3.2")
    _occurrence(conn, "declared:emea-growth", "slide 7", "EMEA growth")
    return conn


def _ctx(conn):
    return triggers._ctx(conn, PROJECT, PLAN, None)


def _node(node_key: str, node_type: str = "metric"):
    return {"node_key": node_key, "qualified_name": node_key, "node_type": node_type,
            "file_path": DECK, "event_types": [], "payloads": [], "run_id": None}


# ── the five paired examples (design doc §5.2) ───────────────────────────────
# (id, the sentence, the node it is about, the answer a correct judge gives,
#  the verdict the system must reach)

def _answer(trigger: bool, basis: str, reason=None) -> str:
    return json.dumps({"trigger": trigger, "reason_in_utterance": reason, "basis": basis}, ensure_ascii=False)


def _span_of(text: str, fragment: str) -> list[int]:
    start = text.index(fragment)
    return [start, start + len(fragment)]


PAIRS = [
    ("correction", "slide 4 转化率写成了 3.02%，应该是 3.2%", "metric:q3_conv", False, None, "silent"),
    ("bare_change", "slide 4 转化率改成 2.8%", "metric:q3_conv", True, None, "ask"),
    ("sync", "把 slide 4 按最新一轮结果更新", "metric:q3_conv", False, None, "silent"),
    ("deleted_conclusion", "把 slide 7 关于 EMEA 增长那段去掉", "declared:emea-growth", True, None, "ask"),
    ("reason_in_the_sentence", "转化率改成 2.8%，Sam 说 EMEA 不算在 Q3 里", "metric:q3_conv", True,
     "Sam 说 EMEA 不算在 Q3 里", "auto"),
]


def _material(prompt: str) -> dict:
    """The payload appended after the prompt text. Matching on the whole prompt
    would match every time: the prompt CARRIES all five example sentences, so
    only the material says which change is being judged."""
    return json.loads(prompt.rsplit("\n\n", 1)[-1])


def _oracle(uid_by_text):
    """A stub runner that answers the five examples the way the labels say —
    keyed on the sentence in the material, not in the prompt. It never calls a model."""
    def runner(prompt, *, model=None, timeout_s=None):
        said = " ".join(u["text"] for u in _material(prompt)["utterances"])
        for name, text, _key, trigger, fragment, _verdict in PAIRS:
            if text in said:
                reason = None
                if fragment:
                    reason = {"utterance_id": uid_by_text[text], "span": _span_of(text, fragment)}
                return _answer(trigger, f"stub oracle: {name}", reason)
        return "I could not tell."
    return runner


# ── C3: the five pairs ───────────────────────────────────────────────────────

@pytest.mark.parametrize("name,text,node_key,trigger,fragment,expected", PAIRS,
                         ids=[p[0] for p in PAIRS])
def test_c3_the_five_paired_examples_are_judged_as_labelled(ledger, name, text, node_key, trigger, fragment, expected):
    uid = _said(ledger, text)
    v = et.judge(_ctx(ledger), _node(node_key), runner=_oracle({text: uid}))
    assert v.verdict == expected
    assert v.trigger is trigger
    if expected == "auto":
        assert v.reason == (uid, *_span_of(text, fragment))
    else:
        assert v.reason is None


def test_a_reason_already_in_the_users_words_is_recorded_stated_and_nobody_is_asked(ledger):
    # The fifth example is a follow-up: the deck is named in the sentence before
    # it, and the reason is in a sentence that never says "slide".
    _said(ledger, "slide 4 上那个数字我们再看一下")
    text = "转化率改成 2.8%，Sam 说 EMEA 不算在 Q3 里"
    uid = _said(ledger, text)
    out = et.evaluate_external(ledger, project=PROJECT, plan_id=PLAN, psg_db_path=None,
                               runner=_oracle({text: uid}), mode="on", commit=True)
    assert out["auto"] == 1 and out["ask"] == 0
    reasons = [r for r in pv.reasons_for_plan(ledger, PLAN) if r["node_key"] == "metric:q3_conv"]
    assert len(reasons) == 1
    r = reasons[0]
    assert r["tier"] == "stated" and r["rule_id"] == "X1" and r["recorded_by"] == "system"
    assert r["verbatim_utterance_id"] == uid
    said = pv.get_utterance(ledger, uid)["text"][r["verbatim_start"]:r["verbatim_end"]]
    assert said == "Sam 说 EMEA 不算在 Q3 里"
    asks = ledger.execute("SELECT COUNT(*) FROM trigger_log WHERE plan_id = ? AND verdict = 'ask'", (PLAN,)).fetchone()[0]
    assert asks == 0


def test_an_answer_that_is_not_json_is_silent_and_says_so(ledger):
    _said(ledger, "slide 4 转化率改成 2.8%")

    def prose(prompt, *, model=None, timeout_s=None):
        return "Hard to say — it might be a correction, it might not."

    v = et.judge(_ctx(ledger), _node("metric:q3_conv"), runner=prose)
    assert v.verdict == "silent" and v.trigger is False
    assert "unparseable" in v.basis


def test_a_span_the_utterance_does_not_contain_records_nothing_and_asks_nothing(ledger):
    text = "slide 4 转化率改成 2.8%"
    uid = _said(ledger, text)

    def out_of_range(prompt, *, model=None, timeout_s=None):
        return _answer(True, "invented a span", {"utterance_id": uid, "span": [0, 4000]})

    out = et.evaluate_external(ledger, project=PROJECT, plan_id=PLAN, psg_db_path=None,
                               runner=out_of_range, mode="on", commit=True)
    assert out["silent"] == 1 and out["ask"] == 0 and out["auto"] == 0
    assert pv.reasons_for_plan(ledger, PLAN) == []
    basis = ledger.execute("SELECT basis FROM trigger_log WHERE plan_id = ? AND path = 'external'", (PLAN,)).fetchone()[0]
    assert "span" in basis


def test_c4_every_verdict_is_one_trigger_log_row_on_the_external_path(ledger):
    text = "slide 4 转化率改成 2.8%"
    uid = _said(ledger, text)
    et.evaluate_external(ledger, project=PROJECT, plan_id=PLAN, psg_db_path=None,
                         runner=_oracle({text: uid}), mode="on", commit=True)
    rows = ledger.execute("SELECT path, rule_id, verdict, basis, node_key FROM trigger_log WHERE plan_id = ? ORDER BY id",
                          (PLAN,)).fetchall()
    assert len(rows) == 1
    path, rule_id, verdict, basis, node_key = rows[0]
    assert (path, verdict, node_key) == ("external", "ask", "metric:q3_conv")
    assert rule_id is None and basis
    et.evaluate_external(ledger, project=PROJECT, plan_id=PLAN, psg_db_path=None,
                         runner=_oracle({text: uid}), mode="on", commit=True)
    assert ledger.execute("SELECT COUNT(*) FROM trigger_log WHERE plan_id = ?", (PLAN,)).fetchone()[0] == 1


def test_an_external_change_needs_both_an_external_node_and_a_word_about_the_artifact(ledger):
    _said(ledger, "slide 4 转化率改成 2.8%")
    ctx = _ctx(ledger)
    assert et.is_external_change(ctx, _node("metric:q3_conv")) is True
    assert et.is_external_change(ctx, {"node_key": "nk_abc", "qualified_name": "pkg.mod.load_orders",
                                       "node_type": "function", "file_path": "pkg/mod.py",
                                       "event_types": ["node_changed"], "payloads": [{}], "run_id": 1}) is False


def test_without_a_word_about_the_artifact_an_external_node_is_not_an_external_change(conn):
    _plan(conn)
    _occurrence(conn, "metric:q3_conv", "slide 4", "3.2")
    _said(conn, "把 compute_conversion 的分母改一下")
    assert et.is_external_change(_ctx(conn), _node("metric:q3_conv")) is False


def test_the_meta_rule_and_the_five_examples_are_in_the_prompt_verbatim():
    prompt = et.prompt_text()
    assert "不确定时，不问" in prompt
    for _name, text, _key, _trigger, _fragment, _verdict in PAIRS:
        assert text in prompt, f"the prompt lost the {_name} example"


def test_the_prompt_carries_the_words_and_the_places_the_number_turned_up(ledger):
    text = "slide 4 转化率改成 2.8%"
    uid = _said(ledger, text)
    prompt = et.prompt_for(_ctx(ledger), _node("metric:q3_conv"))
    assert text in prompt and str(uid) in prompt
    assert "slide 4" in prompt and DECK in prompt
