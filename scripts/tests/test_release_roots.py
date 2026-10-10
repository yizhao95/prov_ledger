"""The release check's root-cause checks (task-level redesign, step 2).

Stage 2's dummy project publishes plan A as a new root and plan B as continuing
A (B exists because A made the discount rate an estimate). The story only
proves the feature if those rows exist and B's headline names A, so stage 2
checks them; stage 0 checks that the plan an agent publishes on its own carries
a root row, and says which kind the agent chose."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "release_e2e"))

import dummy_project as DP  # noqa: E402
import stage0_plugin as P0  # noqa: E402

A, B = "dummy-rollup-a-20260608000000", "dummy-rollup-b-20260819000000"


def _facts(b_root=None, headline=None):
    return {"plan_a": {"plan_id": A},
            "roots": {"a": {"kind": "new", "root_plan_id": A, "continues_plan_id": None},
                      "b": b_root or {"kind": "continues", "root_plan_id": A, "continues_plan_id": A},
                      "b_headline": headline if headline is not None else f" · info  same_root_task:1 same root: {A} (COMPLETED)"}}


def test_the_dummy_story_proves_a_new_root_a_continuation_and_the_headline_line():
    rows = DP.root_checks(_facts())
    assert [ok for ok, _, _ in rows] == [True, True, True]
    assert any("plan B continues plan A" in label for _, label, _ in rows)


def test_a_continuation_that_was_not_recorded_or_not_shown_fails_its_own_check():
    rows = DP.root_checks(_facts(b_root={"kind": "unknown", "root_plan_id": None, "continues_plan_id": None},
                                 headline=" 0 findings"))
    assert [ok for ok, _, _ in rows] == [True, False, False]


def test_stage0_reports_the_kind_the_agent_chose_and_fails_only_when_there_is_no_row():
    def q(rows):
        return lambda sql, params=(): rows
    assert P0.root_check(q([("new", None)]), "p1") == (True, "the agent's plan records a root: new")
    assert P0.root_check(q([("unknown", None)]), "p1") == (True, "the agent's plan records a root: unknown (none named)")
    ok, detail = P0.root_check(q([]), "p1")
    assert not ok and "no plan_root row" in detail
