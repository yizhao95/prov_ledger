#!/usr/bin/env python3
"""Shared dispatcher for executing-plans/scripts/*.sh wrappers.

Each .sh wrapper is ~10 lines: it `exec`s this script with --op <name> + the
user's input file. This module:
  - parses JSON or YAML input
  - validates required fields per op
  - opens the DB (ORCH_DB env var, default real DB)
  - dispatches to the right orchestrator.api or orchestrator.db function
  - prints the result as JSON to stdout
  - exits non-zero on any validation / state-machine / circuit-breaker failure
    with a friendly stderr message naming the offending field

Op enum: start-step, complete-step, fail-step, append-log, deviate,
         record-skill, finish-plan
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

# Prefer the repo-bundled orchestrator-backend (self-contained public clone);
# fall back to the author's internal workspace install otherwise.
_BUNDLED_ORCH = Path(__file__).resolve().parents[3] / "orchestrator-backend"
_DEV_ORCH = Path.home() / "skill-workspace" / "orchestrator"
ORCH_ROOT = _BUNDLED_ORCH if (_BUNDLED_ORCH / "orchestrator" / "__init__.py").exists() else _DEV_ORCH
sys.path.insert(0, str(ORCH_ROOT))

from orchestrator import api, db  # noqa: E402

VALID_SKILL_SOURCES = {"iron-law", "auto-search", "explicit-mention", "deferred-load"}
VALID_STEP_TYPES = {"THINKING", "ANALYSIS", "CODE", "COMMAND", "DOCUMENTATION", "SUB_AGENT"}


def _die(msg: str, code: int = 1) -> None:
    print(f"❌ apply_op: {msg}", file=sys.stderr)
    sys.exit(code)


def _load(path: Path) -> dict:
    if not path.exists():
        _die(f"input file not found: {path}")
    suffix = path.suffix.lower()
    text = path.read_text()
    if suffix == ".json":
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            _die(f"invalid JSON in {path}: {e}")
    if suffix in (".yaml", ".yml"):
        try:
            import yaml
        except ImportError:
            _die("YAML input requires PyYAML")
        try:
            return yaml.safe_load(text) or {}
        except yaml.YAMLError as e:
            _die(f"invalid YAML in {path}: {e}")
    _die(f"unsupported file extension {suffix!r}; use .json/.yaml/.yml")
    return {}  # unreachable


def _require(data: dict, *fields: str) -> None:
    for f in fields:
        if f not in data or data[f] in (None, ""):
            _die(f"missing required field: '{f}'")


def _open_db():
    db_path_env = os.environ.get("ORCH_DB")
    db_path = Path(db_path_env) if db_path_env else None
    conn = db.open_db(db_path) if db_path else db.open_db()
    has_plans = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='Plans'"
    ).fetchone()
    if not has_plans:
        db.run_migrations(conn)
    return conn


# ── op handlers ──────────────────────────────────────────────────────────

def _maybe_auto_review(conn, result: dict) -> None:
    """Fire api.review_and_complete after a non-review step transitions.

    Deterministic auto-promotion: when complete-step or fail-step mutates a
    regular step, we check whether the plan can now be finalized. If so,
    review_and_complete flips both the review step AND the plan to the
    appropriate terminal state — NO LLM involvement.

    Guard: never trigger when the just-mutated step IS the review step itself
    (prevents infinite recursion). The result dict comes from api.complete_step
    / api.fail_step which returns the step row (sqlite3.Row, dict-compatible).
    """
    # result is always a dict here (db.get_step / api.complete_step return dict)
    if result.get("is_review", 0):
        return
    plan_id = result["plan_id"]
    review_outcome = api.review_and_complete(conn, plan_id)
    if review_outcome.get("ready"):
        result["auto_review"] = review_outcome
    elif review_outcome.get("needs_agent_review"):
        # Project-state plan: the deterministic review deferred to an LLM
        # sub-agent. Surface the signal so the main agent (per executing-plans
        # SKILL.md) dispatches the update-project-state-graph review and then
        # finalizes via agent-review-close.sh. The plan stays IN_PROGRESS.
        result["needs_agent_review"] = True
        result["project"] = review_outcome.get("project")
        result["review_step_id"] = review_outcome.get("review_step_id")
        result["review_child_step_id"] = review_outcome.get("review_child_step_id")
        result["auto_review"] = review_outcome


def _op_start_step(conn, data: dict) -> dict:
    _require(data, "step_id")
    step_id = data["step_id"]
    step_type = data.get("type")
    agent_input = data.get("agent_input")
    if step_type and step_type not in VALID_STEP_TYPES:
        _die(f"'type' must be one of {sorted(VALID_STEP_TYPES)}; got {step_type!r}")
    try:
        result = api.start_step(conn, step_id)
    except Exception as e:
        _die(f"start_step failed: {e}")
    if step_type:
        db.set_step_type(conn, step_id, step_type)
        result["step_type"] = step_type
    if agent_input:
        db.set_agent_input(conn, step_id, agent_input)
        result["agent_input_recorded"] = True
    # Inline log on start (Bug 2 fix) — useful when the step kicks off a long
    # background process and you want to capture the launch banner immediately.
    if data.get("log_context"):
        api.append_log(conn, step_id, data["log_context"])
        result["log_chars_appended"] = len(data["log_context"])
    return result


def _op_complete_step(conn, data: dict) -> dict:
    _require(data, "step_id")
    step_id = data["step_id"]
    summary = data.get("summary")
    agent_output = data.get("agent_output")
    log_context = data.get("log_context")
    try:
        result = api.complete_step(conn, step_id)
    except Exception as e:
        _die(f"complete_step failed: {e}")
    if summary:
        db.set_step_summary(conn, step_id, summary)
        result["summary"] = summary
    if agent_output:
        db.set_agent_output(conn, step_id, agent_output)
        result["agent_output_recorded"] = True
    # Inline log capture (Bug 2 fix): no need for a separate append-log call.
    # Goes through the same api.append_log used by append-log.sh, so the same
    # `--- ts ---\n<chunk>\n` delimiter format applies and count_log_entries()
    # in the dashboard keeps counting correctly.
    if log_context:
        api.append_log(conn, step_id, log_context)
        result["log_chars_appended"] = len(log_context)
    # Deterministic auto-promotion (migration 006): if this was the last
    # non-review step in the plan, review_and_complete will flip BOTH the
    # review step AND the plan to COMPLETED — NO LLM call needed.
    _maybe_auto_review(conn, result)
    return result


def _op_fail_step(conn, data: dict) -> dict:
    _require(data, "step_id")
    reason = data.get("reason", "")
    log_context = data.get("log_context")
    try:
        result = api.fail_step(conn, data["step_id"], reason)
    except Exception as e:
        _die(f"fail_step failed: {e}")
    # Inline log on failure is especially valuable — captures the error output
    # that explains WHY the step failed, so the dashboard panel isn't empty.
    if log_context:
        api.append_log(conn, data["step_id"], log_context)
        result["log_chars_appended"] = len(log_context)
    # Auto-trigger: if this failure leaves no PENDING/IN_PROGRESS steps,
    # review_and_complete propagates FAILED to the plan.
    _maybe_auto_review(conn, result)
    return result


def _op_agent_review_close(conn, data: dict) -> dict:
    """Finalize a NEEDS_REVIEW plan on behalf of the review sub-agent.

    outcome 'pass' -> review step COMPLETED + plan COMPLETED.
    outcome 'fail' -> review step FAILED + plan FAILED (details logged).

    Only operates on a plan whose review step is currently NEEDS_REVIEW; any
    other state is rejected so the agent can't double-close or skip the
    deterministic detect phase.
    """
    _require(data, "plan_id", "outcome")
    plan_id = data["plan_id"]
    outcome = data["outcome"]
    if outcome not in ("pass", "fail"):
        _die(f"'outcome' must be 'pass' or 'fail'; got {outcome!r}")
    summary = data.get("summary")
    log_context = data.get("log_context")

    review_row = conn.execute(
        "SELECT step_id, status FROM Steps WHERE plan_id = ? AND is_review = 1 LIMIT 1",
        (plan_id,),
    ).fetchone()
    if review_row is None:
        _die(f"no review step found for plan {plan_id}")
    review_step_id = review_row["step_id"]
    if review_row["status"] != "NEEDS_REVIEW":
        _die(
            f"review step {review_step_id} is {review_row['status']!r}, expected "
            f"NEEDS_REVIEW; nothing to close"
        )

    # Persist details + outcome onto the review step log BEFORE the terminal
    # write, so the dashboard panel shows why the plan closed this way.
    detail = summary or ""
    if log_context:
        detail = f"{detail}\n{log_context}" if detail else log_context
    if detail:
        api.append_log(conn, review_step_id, f"[AGENT-REVIEW {outcome.upper()}] {detail}")
    if summary:
        db.set_step_summary(conn, review_step_id, summary)

    # Keep the tracked child review step (if any) consistent with this close so
    # the two finalize paths (child-driven vs direct close) never disagree.
    child_terminal = "COMPLETED" if outcome == "pass" else "FAILED"
    for child in db.get_children(conn, review_step_id):
        if child["status"] not in ("COMPLETED", "FAILED"):
            db.update_step_status(conn, child["step_id"], child_terminal, set_completed=True)

    try:
        if outcome == "pass":
            api.complete_step(conn, review_step_id)
            db.update_plan_status(conn, plan_id, "COMPLETED")
            new_status = "COMPLETED"
        else:
            api.fail_step(conn, review_step_id, reason=summary or "agent review found gaps")
            db.update_plan_status(conn, plan_id, "FAILED")
            new_status = "FAILED"
    except Exception as e:
        _die(f"agent-review-close failed: {e}")

    return {
        "closed": True,
        "plan_id": plan_id,
        "review_step_id": review_step_id,
        "outcome": outcome,
        "plan_status": new_status,
    }


def _op_append_log(conn, data: dict) -> dict:
    _require(data, "step_id", "text")
    try:
        api.append_log(conn, data["step_id"], data["text"])
    except Exception as e:
        _die(f"append_log failed: {e}")
    return {"appended": True, "step_id": data["step_id"], "chars": len(data["text"])}


def _op_deviate(conn, data: dict) -> dict:
    _require(data, "parent_step_id", "justification", "sub_steps")
    sub_steps = data["sub_steps"]
    if not isinstance(sub_steps, list) or len(sub_steps) == 0:
        _die("'sub_steps' must be a non-empty array")
    try:
        return api.evaluate_and_update_plan(
            conn,
            deviation_detected=True,
            target_step_id=data["parent_step_id"],
            justification=data["justification"],
            new_sub_steps=sub_steps,
        )
    except Exception as e:
        _die(f"deviate failed: {e}")
    return {}  # unreachable


def _op_record_skill(conn, data: dict) -> dict:
    _require(data, "plan_id", "name", "source")
    if data["source"] not in VALID_SKILL_SOURCES:
        _die(f"'source' must be one of {sorted(VALID_SKILL_SOURCES)}; got {data['source']!r}")
    try:
        api.record_skill_activation(
            conn,
            plan_id=data["plan_id"],
            skill_name=data["name"],
            source=data["source"],
            step_id=data.get("step_id"),
            reason=data.get("reason"),
        )
    except Exception as e:
        _die(f"record_skill failed: {e}")
    return {"recorded": True, "plan_id": data["plan_id"], "skill": data["name"]}


def _op_finish_plan(conn, data: dict) -> dict:
    """Unified finish-plan: prefer the dc review_and_complete procedure.

    Behavior:
      - If the plan has a review step (post-migration-006), run
        api.review_and_complete which inspects all non-review steps and decides
        COMPLETED vs FAILED vs not-ready.
      - If the plan is a LEGACY plan (no review row), fall back to plain
        api.complete_plan so back-fill of pre-migration plans keeps working.
      - If review_and_complete returns ready=False (non-terminal steps still
        exist), still call api.complete_plan as a force-finish escape hatch —
        matches the pre-006 behavior so manual back-fill of partial plans
        remains possible. We include the review outcome in the result so the
        caller can see WHY it was force-finished.
    """
    _require(data, "plan_id")
    plan_id = data["plan_id"]
    try:
        review = api.review_and_complete(conn, plan_id)
    except Exception as e:
        _die(f"finish_plan/review_and_complete failed: {e}")
    if review.get("ready"):
        # Procedure already wrote the terminal state. Return the plan row.
        plan_row = db.get_plan(conn, plan_id) or {}
        plan_row["review_outcome"] = review
        return plan_row
    # Either legacy plan (no review row) or partial plan with pending steps.
    # Fall back to unconditional COMPLETED for backward compatibility with the
    # pre-006 finish-plan behavior.
    try:
        plan_row = api.complete_plan(conn, plan_id)
    except Exception as e:
        _die(f"finish_plan fallback failed: {e}")
    plan_row["review_outcome"] = review  # include reason it fell back
    return plan_row


OPS = {
    "start-step":   _op_start_step,
    "complete-step": _op_complete_step,
    "fail-step":    _op_fail_step,
    "append-log":   _op_append_log,
    "deviate":      _op_deviate,
    "record-skill": _op_record_skill,
    "finish-plan":  _op_finish_plan,
    "agent-review-close": _op_agent_review_close,
}


def main() -> None:
    parser = argparse.ArgumentParser(description="Apply one deterministic write-op to the orchestrator DB.")
    parser.add_argument("--op", required=True, choices=list(OPS.keys()))
    parser.add_argument("input_file")
    args = parser.parse_args()

    data = _load(Path(args.input_file))
    conn = _open_db()
    handler = OPS[args.op]
    result = handler(conn, data)

    # Pretty-printed JSON for human consumption (debugging via direct invocation)
    print(json.dumps(result, indent=2, default=str))

    # ── Single-line OK marker (always last stdout line on success) ────────────────
    # Agents pipe through `| tail -1` to detect success/failure deterministically.
    # Failures already exit non-zero with a stderr ❌ line via _die(); successes
    # emit this compact marker so `tail -1 | jq '.ok'` always returns true|null.
    # Includes step_id (or plan_id for record-skill / finish-plan) so the agent
    # can echo-confirm it operated on the right entity.
    marker: dict[str, Any] = {"ok": True, "op": args.op}
    for key in ("step_id", "plan_id"):
        if key in data:
            marker[key] = data[key]
        elif key in result:
            marker[key] = result[key]
    # Propagate the agent-review handoff signal onto the deterministic tail line
    # so `tail -1` consumers (the main agent) see when a registered-project plan
    # needs the update-project-state-graph sub-agent review (NEEDS_REVIEW).
    if result.get("needs_agent_review"):
        marker["needs_agent_review"] = True
        marker["project"] = result.get("project")
        marker["review_step_id"] = result.get("review_step_id")
        marker["review_child_step_id"] = result.get("review_child_step_id")
    print(json.dumps(marker, separators=(",", ":")))


if __name__ == "__main__":
    main()
