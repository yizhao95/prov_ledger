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
from pathlib import Path

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
    ("correction", "slide 4 has the conversion rate as 3.02%, it should be 3.2%", "metric:q3_conv", False, None, "silent"),
    ("bare_change", "change the conversion rate on slide 4 to 2.8%", "metric:q3_conv", True, None, "ask"),
    ("sync", "update slide 4 with the latest round of results", "metric:q3_conv", False, None, "silent"),
    ("deleted_conclusion", "drop the paragraph about EMEA growth on slide 7", "declared:emea-growth", True, None, "ask"),
    ("reason_in_the_sentence", "change the conversion rate to 2.8%, Sam says EMEA does not count in Q3", "metric:q3_conv", True,
     "Sam says EMEA does not count in Q3", "auto"),
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
    _said(ledger, "let us take another look at that number on slide 4")
    text = "change the conversion rate to 2.8%, Sam says EMEA does not count in Q3"
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
    assert said == "Sam says EMEA does not count in Q3"
    asks = ledger.execute("SELECT COUNT(*) FROM trigger_log WHERE plan_id = ? AND verdict = 'ask'", (PLAN,)).fetchone()[0]
    assert asks == 0


def test_an_answer_that_is_not_json_is_silent_and_says_so(ledger):
    _said(ledger, "change the conversion rate on slide 4 to 2.8%")

    def prose(prompt, *, model=None, timeout_s=None):
        return "Hard to say — it might be a correction, it might not."

    v = et.judge(_ctx(ledger), _node("metric:q3_conv"), runner=prose)
    assert v.verdict == "silent" and v.trigger is False
    assert "unparseable" in v.basis


def test_a_span_the_utterance_does_not_contain_records_nothing_and_asks_nothing(ledger):
    text = "change the conversion rate on slide 4 to 2.8%"
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
    text = "change the conversion rate on slide 4 to 2.8%"
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
    _said(ledger, "change the conversion rate on slide 4 to 2.8%")
    ctx = _ctx(ledger)
    assert et.is_external_change(ctx, _node("metric:q3_conv")) is True
    assert et.is_external_change(ctx, {"node_key": "nk_abc", "qualified_name": "pkg.mod.load_orders",
                                       "node_type": "function", "file_path": "pkg/mod.py",
                                       "event_types": ["node_changed"], "payloads": [{}], "run_id": 1}) is False


def test_without_a_word_about_the_artifact_an_external_node_is_not_an_external_change(conn):
    _plan(conn)
    _occurrence(conn, "metric:q3_conv", "slide 4", "3.2")
    _said(conn, "tweak the denominator inside compute_conversion")
    assert et.is_external_change(_ctx(conn), _node("metric:q3_conv")) is False


def test_the_meta_rule_and_the_five_examples_are_in_the_prompt_verbatim():
    prompt = et.prompt_text()
    assert et.META_RULE in prompt
    for _name, text, _key, _trigger, _fragment, _verdict in PAIRS:
        assert text in prompt, f"the prompt lost the {_name} example"


def test_the_prompt_carries_the_words_and_the_places_the_number_turned_up(ledger):
    text = "change the conversion rate on slide 4 to 2.8%"
    uid = _said(ledger, text)
    prompt = et.prompt_for(_ctx(ledger), _node("metric:q3_conv"))
    assert text in prompt and str(uid) in prompt
    assert "slide 4" in prompt and DECK in prompt
# ── Task 1: the switch, the wiring, and the two rates (C5) ───────────────────


def test_with_the_switch_off_no_model_is_asked_and_the_verdict_is_still_logged(ledger):
    _said(ledger, "change the conversion rate on slide 4 to 2.8%")
    calls = []

    def never(prompt, *, model=None, timeout_s=None):
        calls.append(prompt)
        return _answer(True, "this runner should never have been called")

    out = et.evaluate_external(ledger, project=PROJECT, plan_id=PLAN, psg_db_path=None,
                               runner=never, mode="off", commit=True)
    assert calls == []                                    # off means no model call
    assert out["judged"] == 1 and out["silent"] == 1
    path, verdict, basis = ledger.execute(
        "SELECT path, verdict, basis FROM trigger_log WHERE plan_id = ?", (PLAN,)).fetchone()
    assert (path, verdict) == ("external", "silent")      # but the row is still there (C5 needs a denominator)
    assert "off" in basis


def test_with_the_switch_on_the_runner_is_asked_once_per_candidate(ledger):
    text = "change the conversion rate on slide 4 to 2.8%"
    uid = _said(ledger, text)
    calls = []
    oracle = _oracle({text: uid})

    def counted(prompt, *, model=None, timeout_s=None):
        calls.append(prompt)
        return oracle(prompt, model=model, timeout_s=timeout_s)

    et.evaluate_external(ledger, project=PROJECT, plan_id=PLAN, psg_db_path=None,
                         runner=counted, mode="on", commit=True)
    assert len(calls) == 1


def test_c5_the_false_ask_rate_is_the_share_of_asks_the_person_had_nothing_to_say_to(ledger):
    _said(ledger, "change the conversion rate on slide 4 to 2.8%")
    for key, tier in (("metric:q3_conv", "unstated"), ("declared:emea-growth", "stated")):
        et._log(ledger, project=PROJECT, plan_id=PLAN, node_key=key, rule_id=None, verdict="ask", basis="odd")
    pv.insert_reason(ledger, project=PROJECT, plan_id=PLAN, node_key="metric:q3_conv", kind="technical",
                     recorded_by="human", commit=False)                       # nothing to say -> unstated
    said = "Sam says EMEA does not count in Q3"                              # and this one had something to say
    uid = _said(ledger, said)
    pv.insert_reason(ledger, project=PROJECT, plan_id=PLAN, node_key="declared:emea-growth", kind="organizational",
                     verbatim=(uid, 0, len(said)), recorded_by="human", commit=True)
    r = et.rates(ledger, project=PROJECT)
    assert r["asks"] == 2 and r["asks_answered"] == 2 and r["false_asks"] == 1
    assert r["external_false_ask_rate"] == 0.5


def test_c5_the_miss_rate_is_the_share_of_silences_the_person_came_back_to(ledger):
    _said(ledger, "change the conversion rate on slide 4 to 2.8%")
    for key in ("metric:q3_conv", "declared:emea-growth"):
        et._log(ledger, project=PROJECT, plan_id=PLAN, node_key=key, rule_id=None, verdict="silent", basis="not odd")
    volunteered = "that number moved because Sam took EMEA out"
    uid = _said(ledger, volunteered)
    pv.insert_reason(ledger, project=PROJECT, plan_id=PLAN, node_key="metric:q3_conv", kind="organizational",
                     verbatim=(uid, 0, len(volunteered)), recorded_by="human", commit=True)
    r = et.rates(ledger, project=PROJECT)
    assert r["silences"] == 2 and r["misses"] == 1
    assert r["external_miss_rate"] == 0.5


def test_the_rates_print_their_denominators_instead_of_dividing_by_zero(conn):
    r = et.rates(conn, project=PROJECT)
    assert r["asks"] == 0 and r["silences"] == 0
    assert r["external_false_ask_rate"] is None and r["external_miss_rate"] is None


def test_the_external_switch_is_off_until_the_extensions_say_on(tmp_path):
    from orchestrator import extensions
    assert extensions.EMPTY.reasons_external_trigger == "off"
    p = tmp_path / "provledger-extensions.json"
    p.write_text(json.dumps({"version": 1, "reasons": {"external_trigger": "on"}}), encoding="utf-8")
    assert extensions.load(str(p)).reasons_external_trigger == "on"


def test_the_external_switch_refuses_a_value_that_is_neither_off_nor_on(tmp_path):
    from orchestrator import extensions
    p = tmp_path / "provledger-extensions.json"
    p.write_text(json.dumps({"version": 1, "reasons": {"external_trigger": "sometimes"}}), encoding="utf-8")
    with pytest.raises(extensions.ExtensionsError) as e:
        extensions.load(str(p))
    assert "external_trigger" in str(e.value)


def test_the_code_rules_answer_first_and_the_judge_only_sees_what_is_left(ledger, tmp_path):
    """One close, two paths: the analyzer's node is judged by R0–R6 and logged
    path='code'; the deck's number is in no snapshot at all and is judged by the
    model and logged path='external'."""
    import sys as _sys
    _sys.path.insert(0, str(Path(__file__).resolve().parent))
    import _psg_schema as ps
    graph = tmp_path / "proj-state-graph.db"
    g = ps.build(graph)
    ps.add_run(g, 1, plan_id=PLAN)
    ps.add_snapshot(g, 1, "nk_code", "pkg.mod.load_orders")
    ps.add_event(g, 1, 1, "node_added", "nk_code")
    g.commit()
    g.close()
    text = "change the conversion rate on slide 4 to 2.8%"
    uid = _said(ledger, text)
    out = triggers.evaluate(ledger, project=PROJECT, plan_id=PLAN, psg_db_path=str(graph),
                            external_mode="on", external_runner=_oracle({text: uid}), commit=True)
    rows = dict(ledger.execute("SELECT node_key, path FROM trigger_log WHERE plan_id = ?", (PLAN,)).fetchall())
    assert rows["nk_code"] == "code"
    assert rows["metric:q3_conv"] == "external"
    assert out["external"]["ask"] == 1


def test_a_node_a_code_rule_already_answered_is_not_judged_again_by_the_model(ledger):
    text = "change the conversion rate on slide 4 to 2.8%"
    uid = _said(ledger, text)
    pv.insert_reason(ledger, project=PROJECT, plan_id=PLAN, node_key="metric:q3_conv", kind="technical",
                     interpretation="a rule already answered for this node", rule_id="R5",
                     recorded_by="system", commit=True)
    out = et.evaluate_external(ledger, project=PROJECT, plan_id=PLAN, psg_db_path=None,
                               runner=_oracle({text: uid}), mode="on", commit=True)
    assert out["judged"] == 0
