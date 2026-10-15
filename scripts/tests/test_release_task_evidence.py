"""The release check's evidence pass (task-level redesign, step 3b).

Stage 2's dummy story keeps the team's meeting notes in a local file in the
sandbox — the stand-in for a host agent's own notes, mail or chat tool. After
each close the story runs the evidence pass the review skill documents: list
the task slots, search the notes for each slot's hints, hang a found passage
where the slot says (with who said it and their words), and log every slot's
ending. Plan A's root — the user's sentence about the v2 feed — is found in the
data platform sync; plan B continues that root, so it has nothing left to look
for; plan C, a small tidy-up under its own root, searches and finds nothing,
which is recorded as `found_nothing`, not left blank. Stage 2 checks the three
endings before stage 3 grades anything."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "release_e2e"))

import dummy_project as DP  # noqa: E402


def test_the_notes_hold_the_data_platform_sync_and_a_meeting_about_something_else():
    assert "## 2026-05-19 15:00 · data platform sync" in DP.NOTES
    assert DP.NOTES.count("\n## ") >= 2


def test_the_search_finds_the_passage_that_matches_two_hints():
    hit = DP.search_notes(DP.NOTES, ["discount_rate", "load_orders", "v2", "rollup"])
    assert hit is not None
    assert hit["said_by"] == "the data platform lead"
    assert "orders.discount" in hit["excerpt"] and "v2" in hit["excerpt"]
    assert hit["occurred_at"] == "2026-05-19 15:00"
    assert hit["label"] == "data platform sync, 2026-05-19"


def test_the_search_finds_nothing_for_one_hint_or_for_none_that_appear():
    assert DP.search_notes(DP.NOTES, ["rerun_week", "runbook"]) is None
    assert DP.search_notes(DP.NOTES, ["v2"]) is None
    assert DP.search_notes(DP.NOTES, []) is None


def test_v4_adds_a_node_only_plan_c_changes():
    assert "def rerun_week(" in DP.V4["pkg/runbook.py"]
    assert "pkg/runbook.py" not in DP.V3
    assert {k: v for k, v in DP.V4.items() if k != "pkg/runbook.py"} == DP.V3


def test_plan_c_closes_with_one_asserted_task():
    (task,) = DP.plan_c_reasons()["tasks"]
    assert set(task) == {"interpretation"} and "rerun_week" in task["interpretation"]


def _facts(**over):
    f = {"upstream_note": {"utterance_id": 7},
         "evidence": {"a": {"slots": 1, "attached": [{"utterance_id": 7, "said_by": "the data platform lead"}],
                            "found_nothing": 0},
                      "b": {"slots": 0, "attached": [], "found_nothing": 0},
                      "c": {"slots": 1, "attached": [], "found_nothing": 1}}}
    f.update(over)
    return f


def test_stage2_checks_the_three_endings():
    assert [ok for ok, _, _ in DP.evidence_checks(_facts())] == [True, True, True]
    bad = _facts(evidence={"a": {"slots": 1, "attached": [], "found_nothing": 1},
                           "b": {"slots": 1, "attached": [], "found_nothing": 1},
                           "c": {"slots": 1, "attached": [], "found_nothing": 0}})
    assert [ok for ok, _, _ in DP.evidence_checks(bad)] == [False, False, False]
    assert [ok for ok, _, _ in DP.evidence_checks({})] == [False, False, False]


def test_a_passage_hung_on_other_words_than_the_roots_does_not_pass():
    f = _facts()
    f["evidence"]["a"]["attached"] = [{"reason_id": 3, "said_by": "the data platform lead"}]
    assert DP.evidence_checks(f)[0][0] is False
