"""orchestrator.testing.calibration_external — the calibration set and the gate
the external-artifact judge must clear before it may ask anyone anything
(DP phase 5, Task 2; spec §4, C3/C5; the phase-7 gate mechanism, reused).

The five paired examples of design doc §5.2 are the seed: they are the only
items whose right answer is known without anybody labelling anything, because
the design document states it. Everything else comes from a person marking a
real verdict `right` or `wrong` — and a mark is an APPENDED row, because
`trigger_log` refuses UPDATE and a calibration set that can rewrite its own
history calibrates nothing.

The bar is phase 7's, unchanged and on purpose: consistency exactly 1.0 (a
judge that wavers is out), accuracy ≥ 0.9, at least 10 labelled items. Failing
it is a legitimate outcome — the switch simply stays off.

Every test injects a stub runner. No test here reaches a model.
"""
from __future__ import annotations

import json

import pytest

from orchestrator import cli, db, external_trigger as et
from orchestrator.testing import calibration, calibration_external as cx


def _answer(trigger: bool, basis: str, reason=None) -> str:
    return json.dumps({"trigger": trigger, "reason_in_utterance": reason, "basis": basis}, ensure_ascii=False)


def _truthful():
    """A runner that answers every seed the way the design document labels it."""
    def runner(prompt, *, model=None, timeout_s=None):
        material = json.loads(prompt.rsplit("\n\n", 1)[-1])
        said = " ".join(u["text"] for u in material["utterances"])
        uid = material["utterances"][0]["utterance_id"]
        for ex in cx.examples():
            if ex.utterance in said:
                reason = None
                if ex.reason:
                    s = ex.utterance.index(ex.reason)
                    reason = {"utterance_id": uid, "span": [s, s + len(ex.reason)]}
                return _answer(ex.trigger, f"stub: {ex.id}", reason)
        return "no idea"
    return runner


def _wavering():
    """A runner whose answer depends on how many times it has been asked."""
    state = {"n": 0}

    def runner(prompt, *, model=None, timeout_s=None):
        state["n"] += 1
        return _answer(state["n"] % 2 == 0, "stub: coin flip")
    return runner


# ── the five seeds ───────────────────────────────────────────────────────────

def test_the_calibration_set_is_the_five_paired_examples_of_the_design_document():
    ex = cx.examples()
    assert len(ex) == 5
    assert [e.verdict for e in ex] == ["silent", "ask", "silent", "ask", "auto"]
    assert sum(1 for e in ex if e.trigger) == 3          # odd is not the same as ask
    prompt = et.prompt_text()
    for e in ex:
        assert e.utterance in prompt, f"the prompt lost the {e.id} example"


def test_a_runner_that_answers_the_same_way_every_run_is_consistent(tmp_path):
    rep = cx.run(_truthful(), n_runs=3, out_dir=tmp_path)
    assert rep.consistency == 1.0
    assert rep.n_items == 5
    assert rep.accuracy == 1.0


def test_a_runner_that_wavers_is_refused_by_name(tmp_path):
    rep = cx.run(_wavering(), n_runs=3, out_dir=tmp_path)
    assert rep.consistency < 1.0
    ok, detail = cx.gate(out_dir=tmp_path)
    assert ok is False and "consistency" in detail


def test_the_gate_reuses_the_phase_seven_numbers_unchanged():
    assert (cx.MIN_TRUTH, cx.MIN_ACCURACY) == (calibration.MIN_TRUTH, calibration.MIN_ACCURACY)


def test_five_seeds_are_not_ten_labels_so_the_gate_refuses_and_says_so(tmp_path):
    cx.run(_truthful(), n_runs=2, out_dir=tmp_path)
    ok, detail = cx.gate(out_dir=tmp_path)
    assert ok is False and "labelled" in detail and "5" in detail


# ── a person's label is an appended row ──────────────────────────────────────

def _judged(conn, node_key="metric:q3_conv", verdict="ask") -> int:
    return et._log(conn, project="demo", plan_id="P5", node_key=node_key, rule_id=None,
                   verdict=verdict, basis="stub")


def test_a_label_is_appended_next_to_the_verdict_it_marks_never_written_over_it(conn):
    judged = _judged(conn)
    label_id = cx.label(conn, judged, "wrong", note="nothing to say when asked")
    assert label_id != judged
    rows = conn.execute("SELECT id, verdict, user_action, basis FROM trigger_log ORDER BY id").fetchall()
    assert len(rows) == 2
    assert rows[0][2] is None                                  # the judged row is untouched
    assert rows[1][1] == "ask" and rows[1][2] == "wrong"
    assert str(judged) in rows[1][3]


def test_a_label_for_a_verdict_that_does_not_exist_is_refused_by_id(conn):
    with pytest.raises(ValueError) as e:
        cx.label(conn, 4242, "right")
    assert "4242" in str(e.value)


def test_a_label_that_is_neither_right_nor_wrong_is_refused_by_name(conn):
    judged = _judged(conn)
    with pytest.raises(ValueError) as e:
        cx.label(conn, judged, "maybe")
    assert "right" in str(e.value) and "wrong" in str(e.value)


def test_labels_join_the_seeds_and_carry_the_report_over_the_bar(conn, tmp_path):
    for i in range(6):
        cx.label(conn, _judged(conn, node_key=f"metric:m{i}"), "right")
    rep = cx.run(_truthful(), n_runs=2, conn=conn, out_dir=tmp_path)
    assert rep.n_truth == 11                                   # 5 seeds + 6 labelled verdicts
    assert rep.accuracy == 1.0 and rep.consistency == 1.0
    ok, detail = cx.gate(out_dir=tmp_path)
    assert ok is True and "11" in detail


def test_a_wrong_label_lowers_the_accuracy_it_is_meant_to_measure(conn, tmp_path):
    for i in range(9):
        cx.label(conn, _judged(conn, node_key=f"metric:m{i}"), "right")
    cx.label(conn, _judged(conn, node_key="metric:bad"), "wrong")
    rep = cx.run(_truthful(), n_runs=2, conn=conn, out_dir=tmp_path)
    assert rep.n_truth == 15 and rep.accuracy == round(14 / 15, 4)
    ok, _ = cx.gate(out_dir=tmp_path)
    assert ok is True                                          # 0.933 >= 0.9


# ── the CLI ──────────────────────────────────────────────────────────────────

def test_trigger_eval_writes_a_report_and_prints_the_gates_verdict(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("PROVLEDGER_ARBITER_EVAL_DIR", str(tmp_path))
    assert cli.main(["trigger", "eval", "--runner", "stub-truthful", "--n-runs", "2"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["consistency"] == 1.0
    assert out["gate"]["ok"] is False and "labelled" in out["gate"]["detail"]
    assert (tmp_path / "external_trigger.json").exists()


def test_trigger_label_records_the_mark_and_prints_the_row_it_marks(tmp_path, capsys, monkeypatch):
    dbp = tmp_path / "orch.db"
    monkeypatch.setenv("ORCH_DB", str(dbp))
    c = db.open_db(dbp)
    db.run_migrations(c)
    judged = _judged(c)
    c.commit()
    c.close()
    assert cli.main(["trigger", "label", str(judged), "wrong"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["marks"] == judged and out["user_action"] == "wrong"
