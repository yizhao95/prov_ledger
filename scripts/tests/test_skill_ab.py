"""skill_ab — the same questions, asked of two versions of the skills (skill redesign, 2026-10-06).

A real session is nondeterministic and the judge is a model, so one run per
question says little; a change to a skill is judged by asking every question N
times of each version and comparing what the judge found. `aggregate` folds those
rows into the comparison: key points hit, claims resting on no record, whether
the runs of one question agreed, and the two clarity readings the user asked for
(the first sentence answers; cause and effect told in date order).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "release_e2e"))


@pytest.fixture
def AB():
    import skill_ab
    return skill_ab


def _row(variant, run, qid, hits, points, unsupported=False, finding=False, first=True, dated=True):
    return {"variant": variant, "run": run, "id": qid, "outcome": "ok", "hits": hits, "points": points,
            "unsupported": unsupported, "verdict": "FINDING" if finding else "OK",
            "clarity": {"answer_first": first, "date_order": dated}}


def test_each_variant_is_folded_per_question_over_its_runs(AB):
    rows = [_row("old", 1, "Q1", 1, 2, unsupported=True, finding=True, first=False),
            _row("old", 2, "Q1", 2, 2),
            _row("new", 1, "Q1", 2, 2), _row("new", 2, "Q1", 2, 2)]
    out = AB.aggregate(rows, runs=2, questions=["Q1"])
    old, new = out["old"]["Q1"], out["new"]["Q1"]
    assert (old["hits"], old["points"], old["unsupported"], old["agree"]) == (3, 4, 1, False)
    assert (new["hits"], new["points"], new["unsupported"], new["agree"]) == (4, 4, 0, True)
    assert old["answer_first"] == 1 and new["answer_first"] == 2


def test_the_totals_add_up_per_variant(AB):
    rows = [_row("new", 1, "Q1", 2, 2), _row("new", 1, "Q2", 0, 1, unsupported=True, finding=True, dated=False)]
    t = AB.aggregate(rows, runs=1, questions=["Q1", "Q2"])["new"]["_total"]
    assert (t["hits"], t["points"], t["unsupported"], t["findings"], t["date_order"], t["runs"]) == (2, 3, 1, 1, 1, 2)


def test_a_run_that_produced_no_answer_is_reported_not_averaged_in(AB):
    rows = [_row("old", 1, "Q1", 2, 2), {"variant": "old", "run": 2, "id": "Q1", "outcome": "timeout"}]
    out = AB.aggregate(rows, runs=2, questions=["Q1"])
    assert out["old"]["Q1"]["hits"] == 2 and out["old"]["Q1"]["runs"] == 1
    assert out["old"]["_missing"] == [("Q1", 2)]


def test_a_question_never_asked_is_missing_for_every_run(AB):
    out = AB.aggregate([], runs=2, questions=["Q1"], variants=["old"])
    assert out["old"]["_missing"] == [("Q1", 1), ("Q1", 2)]
