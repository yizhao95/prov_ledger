"""Phase 1.4 — drift -> LLM decision -> action -> re-verify loop (stubbed LLM).

The loop never mutates domain state itself: `apply_action` is the injection point
that, in production, drives the existing writing-plans -> executing-plans flow.
Here it's a stub so the loop is testable without a live model or real data.
"""
from __future__ import annotations

from orchestrator import data_loop, db


def _profile(col, dtype, distinct=5, rows=10, dataset="train"):
    return [{"dataset": dataset, "column_name": col, "dtype": dtype,
             "null_frac": 0.0, "distinct_count": distinct, "row_count": rows}]


def test_loop_records_decision_and_verifies_fix(conn):
    # Example B: a label flips numeric -> string feeding predict.
    prev = _profile("label", "int64")
    curr = _profile("label", "object")
    applied = {}

    def decide(ctx):
        # the LLM sees the drift + downstream consumers + the task goal
        assert ctx["drift"]["kind"] == "dtype_changed"
        assert any("predict" in c for c in ctx["downstream_consumers"])
        return {"action": "coerce_upstream",
                "decision": "cast label back to int upstream",
                "rationale": "predict needs numeric labels; the string cast was a bug",
                "failure": True}

    def apply_action(action, drift):
        applied["action"] = action  # prod: this drives publish -> run-step

    def reprofile():
        return _profile("label", "int64")  # after the fix, dtype is numeric again

    results = data_loop.run_data_decision_loop(
        conn, project="proj", plan_id="p1", step_id="s1",
        prev_profile=prev, curr_profile=curr,
        downstream_consumers=["model.predict"], task_goal="train a classifier",
        decide=decide, apply_action=apply_action, reprofile=reprofile)

    assert applied["action"] == "coerce_upstream"
    assert len(results) == 1
    assert results[0]["outcome"] == "resolved"          # drift cleared on re-profile

    rows = db.get_llm_decisions(conn, dataset="train", project="proj")
    assert len(rows) == 1 and rows[0]["action"] == "coerce_upstream"
    # recorded as a ledger anti-pattern (failure) so it surfaces next time
    kind = conn.execute(
        "SELECT kind FROM LedgerEntries WHERE source='llm_decision'").fetchone()[0]
    assert kind == "anti_pattern"


def test_loop_unresolved_when_fix_does_not_clear_drift(conn):
    prev = _profile("x", "int64")
    curr = _profile("x", "object")

    def decide(ctx):
        return {"action": "coerce_upstream", "decision": "try cast", "rationale": "r"}

    def reprofile():
        return _profile("x", "object")  # still wrong -> not cleared

    results = data_loop.run_data_decision_loop(
        conn, project="p", plan_id="p1", step_id="s1",
        prev_profile=prev, curr_profile=curr, downstream_consumers=[],
        task_goal="t", decide=decide, apply_action=lambda a, d: None,
        reprofile=reprofile)
    assert results[0]["outcome"] == "unresolved"


def test_loop_adapt_downstream_is_a_decision_not_failure(conn):
    prev = _profile("c", "int64")
    curr = _profile("c", "object")

    def decide(ctx):
        return {"action": "adapt_downstream",
                "decision": "add an encoder before predict",
                "rationale": "c is categorical by design"}

    results = data_loop.run_data_decision_loop(
        conn, project="p", plan_id="p1", step_id="s1",
        prev_profile=prev, curr_profile=curr, downstream_consumers=["predict"],
        task_goal="t", decide=decide, apply_action=lambda a, d: None,
        reprofile=lambda: _profile("c", "object"))
    # intentional change -> ledger 'decision', not 'anti_pattern'
    kind = conn.execute(
        "SELECT kind FROM LedgerEntries WHERE source='llm_decision'").fetchone()[0]
    assert kind == "decision"


def test_loop_no_drift_no_decision(conn):
    prof = _profile("a", "int64")
    results = data_loop.run_data_decision_loop(
        conn, project="p", plan_id="p1", step_id="s1",
        prev_profile=prof, curr_profile=prof, downstream_consumers=[],
        task_goal="t", decide=lambda c: {"action": "halt", "decision": "x"},
        apply_action=lambda a, d: None, reprofile=lambda: prof)
    assert results == []
    assert db.get_llm_decisions(conn) == []
