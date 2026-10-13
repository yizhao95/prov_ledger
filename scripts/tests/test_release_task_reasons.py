"""The release check's task reasons (task-level redesign, step 3a).

Stage 2's dummy story closes plan A with one stated task reason — the user's
own sentence about the v2 feed — and plan B with an unstated task: nobody said
why B was done. V3 adds estimate_note(), a node only plan B changes, so the
honest-absence questions (Q4, Q7) ask about a node whose only task reason is
B's unstated one; plan A's reason does not reach it, because plan A never
changed it. Stage 2 checks those rows exist before stage 3 grades anything."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "release_e2e"))
BACKEND_TESTS = ROOT / "orchestrator-backend" / "tests"          # for _psg_schema
sys.path.insert(0, str(BACKEND_TESTS))

import _psg_schema as ps  # noqa: E402
import dummy_project as DP  # noqa: E402

A, B = "dummy-rollup-a-20260608000000", "dummy-rollup-b-20260819000000"


def test_v3_adds_a_node_that_only_plan_b_changes():
    assert "def estimate_note(" in DP.V3["pkg/rollup.py"]
    assert "estimate_note" not in DP.V2["pkg/rollup.py"] and "estimate_note" not in DP.V1["pkg/rollup.py"]


def test_plan_a_closes_with_one_stated_task_and_plan_b_with_an_unstated_one():
    assert DP.plan_a_reasons(7) == {"tasks": [{"utterance_id": 7, "span": [0, DP.SAID.index(" — ")]}]}
    assert DP.PLAN_B_REASONS == "unstated"


def test_the_node_only_plan_b_changed_is_read_from_the_graph(tmp_path):
    graph = tmp_path / "g.db"
    c = ps.build(graph)
    ps.add_run(c, 1, plan_id="P0")
    for i, (k, qn) in enumerate((("nk_r", "pkg.rollup.weekly_report"), ("nk_d", "pkg.rollup.discount_rate")), 1):
        ps.add_snapshot(c, 1, k, qn)
        ps.add_event(c, 1, i, "node_added", k)
    ps.add_run(c, 2, plan_id=A)
    ps.add_snapshot(c, 2, "nk_r", "pkg.rollup.weekly_report", struct_sig="s2")
    ps.add_event(c, 2, 1, "node_changed", "nk_r", '{"changed": ["struct_sig"]}')
    ps.add_event(c, 2, 2, "node_changed", "nk_d", '{"changed": ["docstring"]}')     # not a change
    ps.add_run(c, 3, plan_id=B)
    ps.add_snapshot(c, 3, "nk_r", "pkg.rollup.weekly_report", struct_sig="s3")
    ps.add_event(c, 3, 1, "node_changed", "nk_r", '{"changed": ["struct_sig"]}')
    ps.add_snapshot(c, 3, "nk_n", "pkg.rollup.estimate_note")
    ps.add_event(c, 3, 2, "node_added", "nk_n")
    c.commit(); c.close()
    assert DP.changed_only_by(graph, B, others=[A]) == ["pkg.rollup.estimate_note"]
    assert DP.changed_only_by(graph, A, others=[B]) == []


def _facts(**over):
    f = {"plan_a": {"plan_id": A, "ran_on": "2026-10-12"}, "plan_b": {"plan_id": B, "ran_on": "2026-10-13"},
         "upstream_note": {"occurred_at": "2026-06-01 09:00:00"},
         "never_explained": "pkg.rollup.estimate_note", "never_explained_by": B,
         "ledger_shape": {"unstated_tasks": 1}}
    f.update(over)
    return f


def test_stage2_checks_the_unstated_task_and_the_node_it_left_unexplained():
    assert [ok for ok, _, _ in DP.absence_checks(_facts())] == [True, True]
    assert [ok for ok, _, _ in DP.absence_checks(_facts(ledger_shape={"unstated_tasks": 0}, never_explained=None))] == [False, False]
    assert any("task" in label and "unstated" in label for _, label, _ in DP.absence_checks(_facts()))


def test_q4_and_q7_ask_about_the_node_only_plan_b_changed_and_keep_plan_a_out_of_it():
    qs = {q["id"]: q for q in DP.questions({**_facts(), "declared": {"decided_by": "x", "decided_on": "y"}})}
    assert "estimate_note" in qs["Q4-absence"]["question"] and "estimate_note" in qs["Q7-receipts-absence"]["question"]
    q7 = " ".join(qs["Q7-receipts-absence"]["must"])
    assert "2026-10-13" in q7 and "orders.discount" not in q7          # B's day; A's words are not this node's reason
    assert any("v2" in m and "estimate_note" in m for m in qs["Q7-receipts-absence"]["must_not"])
