"""The data-decision loop (Phase 1.4): drift -> LLM decision -> action -> re-verify.

This is the testable core. The three callables are the seams where the real world
plugs in — they are injected so the loop runs without a live model or real data:

  decide(ctx) -> {action, decision, rationale, failure?}
      ctx = {drift, downstream_consumers, task_goal}. In production this is a
      SUB_AGENT decision step (an LLM), like api._open_agent_review.
  apply_action(action, drift) -> None
      Applies the chosen fix. In production this drives the EXISTING flow
      (writing-plans publish -> executing-plans run-step) so it passes the same
      tests + circuit breakers + contract gates. The loop never mutates domain
      state directly — only `apply_action` does, through the backbone.
  reprofile() -> list[profile rows]
      Re-profiles after the fix so we can verify the drift cleared.

Every decision is recorded via db.insert_llm_decision (which also auto-syncs the
ledger). `action == "coerce_upstream"` / `"halt"` are treated as failures
(anti-patterns to not repeat); `"adapt_downstream"` is an intentional decision.
"""
from __future__ import annotations

from typing import Callable

from . import db, drift as drift_mod

_FAILURE_ACTIONS = {"coerce_upstream", "halt"}


def run_data_decision_loop(
    conn, *, project: str | None, plan_id: str | None, step_id: str | None,
    prev_profile: list[dict], curr_profile: list[dict],
    downstream_consumers: list, task_goal: str,
    decide: Callable[[dict], dict],
    apply_action: Callable[[str, dict], None],
    reprofile: Callable[[], list[dict]],
) -> list[dict]:
    """Run the loop over the drifts between prev/curr profiles. Returns one result
    per drift: {drift, decision_id, action, outcome}."""
    drifts = drift_mod.detect_drift(prev_profile, curr_profile)
    results: list[dict] = []

    for d in drifts:
        verdict = decide({
            "drift": d,
            "downstream_consumers": downstream_consumers,
            "task_goal": task_goal,
        })
        action = verdict.get("action", "halt")

        outcome = "noop"
        if action in ("coerce_upstream", "adapt_downstream"):
            apply_action(action, d)                         # through the backbone
            new_profile = reprofile()
            cleared = not _column_still_drifted(prev_profile, new_profile, d["column"])
            outcome = "resolved" if cleared else "unresolved"
        elif action == "halt":
            outcome = "halted"

        failure = verdict.get("failure")
        if failure is None:
            failure = action in _FAILURE_ACTIONS

        decision_id = db.insert_llm_decision(
            conn, project=project, plan_id=plan_id, step_id=step_id,
            dataset=d.get("dataset"), column=d.get("column"), drift_kind=d.get("kind"),
            observed_before=str(d.get("before")), observed_after=str(d.get("after")),
            decision=verdict.get("decision", action),
            rationale=verdict.get("rationale"), action=action, outcome=outcome,
            failure=bool(failure),
        )
        results.append({"drift": d, "decision_id": decision_id,
                        "action": action, "outcome": outcome})

    return results


def _column_still_drifted(prev_profile, new_profile, column) -> bool:
    """True if the named column still shows any drift after the fix."""
    return any(d.get("column") == column
               for d in drift_mod.detect_drift(prev_profile, new_profile))
