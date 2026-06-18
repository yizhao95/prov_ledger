#!/usr/bin/env python3
"""orchestrator-cli.py — shell wrapper for the orchestration harness.

Subcommands:
  init-plan        Create a new plan with N initial steps
  start-step       PENDING → IN_PROGRESS  (validates transition)
  complete-step    IN_PROGRESS → COMPLETED  (immutable after this)
  fail-step        any → FAILED  (with optional reason)
  append-log       append telemetry chunk to a step's log_context
  show-plan        pretty-print plan + steps + a sample log
  list-plans       all plans with status
  regen-markdown   write human-readable .md view from DB rows
  evaluate         register a deviation + (optionally) insert sub-steps
  record-skill     record a skill activation against a plan (and optionally a step)
  list-skills      list all skill activations for a plan

All commands enforce state machine + circuit breakers. Non-zero exit on violation.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

# Allow running as `python orchestrator-cli.py ...` without install
sys.path.insert(0, str(Path(__file__).resolve().parent / "orchestrator"))

from orchestrator import api, db  # noqa: E402
from orchestrator.circuit_breakers import CircuitBreakerError, HardStop  # noqa: E402
from orchestrator.state_machine import InvalidTransitionError  # noqa: E402


# ── Helpers ───────────────────────────────────────────────────────────────────
STATUS_EMOJI = {
    "PENDING": "⏳", "STARTING": "🟡", "IN_PROGRESS": "🚧",
    "COMPLETED": "✅", "FAILED": "❌",
}
PLAN_STATUS_EMOJI = {"IN_PROGRESS": "🚧", "COMPLETED": "✅", "FAILED": "❌"}


def _open(args):
    path = Path(args.db) if args.db else None
    conn = db.open_db(path) if path else db.open_db()
    db.run_migrations(conn)
    return conn


def _print_json(payload):
    print(json.dumps(payload, indent=2, default=str))


# ── Subcommand handlers ───────────────────────────────────────────────────────
def cmd_init_plan(args, conn):
    # Parse --skill entries: each must be "name:source" (reason added later via record-skill)
    skills_activated = []
    for raw in (args.skill or []):
        if ":" not in raw:
            print(f"❌ --skill must be 'name:source', got {raw!r}", file=sys.stderr)
            sys.exit(1)
        name, source = raw.split(":", 1)
        skills_activated.append({"skill_name": name.strip(), "source": source.strip()})

    result = api.initialize_plan(
        conn,
        original_goal=args.goal,
        initial_steps=args.steps,
        plan_id_prefix=args.prefix,
        max_revisions=args.max_revisions,
        user_query=args.user_query,
        skills_activated=skills_activated or None,
    )
    if skills_activated:
        result["skills_recorded"] = [s["skill_name"] for s in skills_activated]
    _print_json(result)


def cmd_record_skill(args, conn):
    """Record one skill activation against a plan (optionally tied to a step)."""
    activation_id = api.record_skill_activation(
        conn,
        plan_id=args.plan_id,
        skill_name=args.name,
        source=args.source,
        step_id=args.step,
        reason=args.reason,
    )
    _print_json({
        "recorded": True,
        "activation_id": activation_id,
        "plan_id": args.plan_id,
        "skill_name": args.name,
        "source": args.source,
        "step_id": args.step,
    })


def cmd_list_skills(args, conn):
    """List all skill activations for a plan, plus per-skill counts."""
    plan = db.get_plan(conn, args.plan_id)
    if not plan:
        print(f"❌ plan_id not found: {args.plan_id}", file=sys.stderr)
        sys.exit(2)
    rows = db.get_skill_activations(conn, args.plan_id)
    counts = db.count_skill_uses_for_plan(conn, args.plan_id)
    if not rows:
        print(f"(no skill activations recorded for {args.plan_id})")
        return
    print(f"\n🧠 Skill activations for {args.plan_id} ({len(rows)} total)")
    print(f"{'Skill':<35} {'Source':<18} {'Step':<35} {'Activated At':<22} Reason")
    print(f"{'-'*35} {'-'*18} {'-'*35} {'-'*22} {'-'*30}")
    for r in rows:
        reason = (r["reason"] or "")[:30]
        step = r["step_id"] or "(init-time)"
        print(f"{r['skill_name']:<35} {r['source']:<18} {step:<35} {r['activated_at']:<22} {reason}")
    print(f"\n📊 Counts: {counts}")


def cmd_start_step(args, conn):
    step = api.start_step(conn, args.step_id)
    if getattr(args, "type", None):
        db.set_step_type(conn, args.step_id, args.type)
    if getattr(args, "agent_input", None):
        db.set_agent_input(conn, args.step_id, args.agent_input)
    _print_json({"started": step["step_id"], "status": step["status"], "started_at": step["started_at"],
                 "step_type": getattr(args, "type", None)})


def cmd_complete_step(args, conn):
    step = api.complete_step(conn, args.step_id)
    if getattr(args, "agent_output", None):
        db.set_agent_output(conn, args.step_id, args.agent_output)
    if getattr(args, "summary", None):
        db.set_step_summary(conn, args.step_id, args.summary)
    _print_json({"completed": step["step_id"], "status": step["status"], "completed_at": step["completed_at"]})


def cmd_set_step_type(args, conn):
    db.set_step_type(conn, args.step_id, args.type)
    print(f"✅ step {args.step_id} type → {args.type}")


def cmd_set_agent_input(args, conn):
    text = args.text if args.text else sys.stdin.read()
    db.set_agent_input(conn, args.step_id, text)
    print(f"✅ step {args.step_id} agent_input set ({len(text)} chars)")


def cmd_set_agent_output(args, conn):
    text = args.text if args.text else sys.stdin.read()
    db.set_agent_output(conn, args.step_id, text)
    print(f"✅ step {args.step_id} agent_output set ({len(text)} chars)")


def cmd_set_summary(args, conn):
    """Set the human-curated 1-line summary for a step.

    Distinct from append-log (which holds raw machine output).
    Convention: call this RIGHT BEFORE complete-step, with a 1-sentence
    description of what the step accomplished.
    """
    text = args.text if args.text else sys.stdin.read()
    db.set_step_summary(conn, args.step_id, text)
    print(f"✅ step {args.step_id} summary set ({len(text)} chars)")


def cmd_fail_step(args, conn):
    step = api.fail_step(conn, args.step_id, reason=args.reason or "")
    _print_json({"failed": step["step_id"], "status": step["status"], "reason": args.reason or ""})


def cmd_append_log(args, conn):
    raw = args.text if args.text else sys.stdin.read()
    final_log = api.append_log(conn, args.step_id, raw)
    print(f"✅ appended {len(raw)} chars → log_context now {len(final_log)} chars")


def cmd_evaluate(args, conn):
    result = api.evaluate_and_update_plan(
        conn,
        deviation_detected=args.deviation,
        target_step_id=args.target,
        justification=args.justification,
        new_sub_steps=args.sub_steps or None,
    )
    _print_json(result)


def cmd_complete_plan(args, conn):
    plan = api.complete_plan(conn, args.plan_id)
    _print_json({"completed": plan["plan_id"], "status": plan["status"], "completed_at": plan["completed_at"]})


def cmd_show_plan(args, conn):
    plan = db.get_plan(conn, args.plan_id)
    if not plan:
        print(f"❌ plan_id not found: {args.plan_id}", file=sys.stderr)
        sys.exit(2)
    steps = db.get_steps(conn, args.plan_id)
    pe = PLAN_STATUS_EMOJI.get(plan["status"], "?")
    print(f"\n{pe} Plan: {plan['plan_id']}")
    print(f"   Goal: {plan['original_goal']}")
    print(f"   Status: {plan['status']}  |  revisions: {plan['revision_count']}/{plan['max_revisions']}")
    print(f"   Created: {plan['created_at']}  |  Completed: {plan['completed_at'] or '—'}")
    print(f"\n   {'Step ID':<55} {'Status':<14} {'Depth':<6} {'Started':<22} {'Completed':<22}")
    print(f"   {'-'*55} {'-'*14} {'-'*6} {'-'*22} {'-'*22}")
    for s in steps:
        em = STATUS_EMOJI.get(s["status"], "?")
        print(f"   {s['step_id']:<55} {em} {s['status']:<11} {s['depth_level']:<6} "
              f"{(s['started_at'] or '—'):<22} {(s['completed_at'] or '—'):<22}")
    # show first non-empty log
    for s in steps:
        if s["log_context"]:
            print(f"\n   ── Sample log (step {s['step_id']}) ──")
            for line in s["log_context"].splitlines()[:15]:
                print(f"     {line}")
            break


def cmd_list_plans(args, conn):
    plans = db.list_plans(conn)
    if not plans:
        print("(no plans yet)")
        return
    print(f"\n{'Plan ID':<55} {'Status':<14} {'Rev':<6} {'Steps':<6} Created")
    print(f"{'-'*55} {'-'*14} {'-'*6} {'-'*6} {'-'*20}")
    for p in plans:
        n_steps = conn.execute(
            "SELECT COUNT(*) AS n FROM Steps WHERE plan_id = ?", (p["plan_id"],)
        ).fetchone()["n"]
        em = PLAN_STATUS_EMOJI.get(p["status"], "?")
        print(f"{p['plan_id']:<55} {em} {p['status']:<11} {p['revision_count']:<6} {n_steps:<6} {p['created_at']}")


def cmd_regen_markdown(args, conn):
    plan = db.get_plan(conn, args.plan_id)
    if not plan:
        print(f"❌ plan_id not found: {args.plan_id}", file=sys.stderr)
        sys.exit(2)
    steps = db.get_steps(conn, args.plan_id)
    out_path = Path(args.out) if args.out else (
        Path.home() / "skill-workspace" / "plans" / f"{args.plan_id}.md"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)

    pe = PLAN_STATUS_EMOJI.get(plan["status"], "?")
    lines = [
        f"# Plan: {plan['plan_id']} (regenerated view)",
        "",
        f"> **Status:** {pe} {plan['status']}",
        f"> **Goal:** {plan['original_goal']}",
        f"> **revision_count:** {plan['revision_count']} / max {plan['max_revisions']}",
        f"> **Created:** {plan['created_at']}",
        f"> **Completed:** {plan['completed_at'] or '—'}",
        "",
        "> ⚠️  This file is REGENERATED from `~/skill-workspace/orchestrator.db`.",
        ">     Do not edit by hand — your changes will be overwritten.",
        "",
        "## Status Dashboard",
        "",
        "| Step ID | Description | Status | Depth | Started | Completed |",
        "|---|---|---|---|---|---|",
    ]
    for s in steps:
        em = STATUS_EMOJI.get(s["status"], "?")
        desc = s["description"].replace("|", "\\|")
        lines.append(
            f"| `{s['step_id']}` | {desc} | {em} {s['status']} | {s['depth_level']} | "
            f"{s['started_at'] or '—'} | {s['completed_at'] or '—'} |"
        )
    lines.append("")
    lines.append("## Logs")
    lines.append("")
    for s in steps:
        if s["log_context"]:
            lines.append(f"### {s['step_id']}")
            lines.append("```")
            lines.append(s["log_context"])
            lines.append("```")
            lines.append("")
    out_path.write_text("\n".join(lines))
    print(f"✅ regenerated {out_path}")


# ── argparse wiring ───────────────────────────────────────────────────────────
def build_parser():
    p = argparse.ArgumentParser(prog="orchestrator-cli", description=__doc__.splitlines()[0])
    p.add_argument("--db", help="SQLite path (default: ~/skill-workspace/orchestrator.db)")
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("init-plan", help="Create a new plan")
    sp.add_argument("--goal", required=True)
    sp.add_argument("--steps", nargs="+", required=True, metavar="STEP_DESC")
    sp.add_argument("--prefix", default="plan")
    sp.add_argument("--max-revisions", type=int, default=5)
    sp.add_argument("--user-query", default=None,
                    help="Verbatim user prompt that triggered this plan (used as history-page title)")
    sp.add_argument("--skill", action="append", metavar="NAME:SOURCE",
                    help="Record a skill activated at plan-init time. Repeatable. "
                         "Format: 'skill-name:source' where source is one of "
                         "auto-search/explicit-mention/iron-law/deferred-load. "
                         "Example: --skill writing-plans:iron-law --skill tdd:iron-law")
    sp.set_defaults(func=cmd_init_plan)

    sp = sub.add_parser("start-step", help="Mark step IN_PROGRESS")
    sp.add_argument("step_id")
    sp.add_argument("--type", choices=sorted(db.VALID_STEP_TYPES),
                    help="Set step_type at start (THINKING/DOCUMENTATION/CODE/COMMAND/SUB_AGENT/ANALYSIS)")
    sp.add_argument("--agent-input", help="For SUB_AGENT steps: the input prompt sent to the sub-agent")
    sp.set_defaults(func=cmd_start_step)

    sp = sub.add_parser("complete-step", help="Mark step COMPLETED")
    sp.add_argument("step_id")
    sp.add_argument("--agent-output", help="For SUB_AGENT steps: the response returned by the sub-agent")
    sp.add_argument("--summary", help="1-line human-curated description of what this step accomplished (distinct from raw log)")
    sp.set_defaults(func=cmd_complete_step)

    sp = sub.add_parser("fail-step", help="Mark step FAILED")
    sp.add_argument("step_id")
    sp.add_argument("--reason", help="Failure reason (appended to log)")
    sp.set_defaults(func=cmd_fail_step)

    sp = sub.add_parser("append-log", help="Append telemetry chunk to step log")
    sp.add_argument("step_id")
    sp.add_argument("--text", help="Log text (default: read from stdin)")
    sp.set_defaults(func=cmd_append_log)

    sp = sub.add_parser("evaluate", help="Register a deviation + (optionally) insert sub-steps")
    sp.add_argument("--deviation", action="store_true")
    sp.add_argument("--target", help="target_step_id")
    sp.add_argument("--justification")
    sp.add_argument("--sub-steps", nargs="*", metavar="DESC")
    sp.set_defaults(func=cmd_evaluate)

    sp = sub.add_parser("complete-plan", help="Mark plan COMPLETED")
    sp.add_argument("plan_id")
    sp.set_defaults(func=cmd_complete_plan)

    sp = sub.add_parser("show-plan", help="Pretty-print plan + steps")
    sp.add_argument("plan_id")
    sp.set_defaults(func=cmd_show_plan)

    sp = sub.add_parser("list-plans", help="List all plans")
    sp.set_defaults(func=cmd_list_plans)

    sp = sub.add_parser("regen-markdown", help="Regenerate human-readable .md from DB")
    sp.add_argument("plan_id")
    sp.add_argument("--out", help="Output path (default: ~/skill-workspace/plans/<plan_id>.md)")
    sp.set_defaults(func=cmd_regen_markdown)

    # v2: step type + agent I/O standalone setters
    sp = sub.add_parser("set-step-type", help="Set/change a step's type")
    sp.add_argument("step_id")
    sp.add_argument("--type", choices=sorted(db.VALID_STEP_TYPES), required=True)
    sp.set_defaults(func=cmd_set_step_type)

    sp = sub.add_parser("set-agent-input", help="Record sub-agent input prompt for a step")
    sp.add_argument("step_id")
    sp.add_argument("--text", help="Input text (default: read from stdin)")
    sp.set_defaults(func=cmd_set_agent_input)

    sp = sub.add_parser("set-agent-output", help="Record sub-agent output response for a step")
    sp.add_argument("step_id")
    sp.add_argument("--text", help="Output text (default: read from stdin)")
    sp.set_defaults(func=cmd_set_agent_output)

    sp = sub.add_parser("set-summary", help="Set the human-curated 1-line summary for a step (distinct from raw log)")
    sp.add_argument("step_id")
    sp.add_argument("--text", help="Summary text (default: read from stdin)")
    sp.set_defaults(func=cmd_set_summary)

    # v3: skill activations
    sp = sub.add_parser("record-skill", help="Record a skill activation against a plan (and optionally a step)")
    sp.add_argument("plan_id")
    sp.add_argument("--name", required=True, help="Skill name (e.g. 'test-driven-development')")
    sp.add_argument("--source", required=True,
                    choices=sorted(db.VALID_SKILL_SOURCES),
                    help="How the skill was triggered")
    sp.add_argument("--step", default=None,
                    help="Optional step_id if activated mid-execution under a specific step")
    sp.add_argument("--reason", default=None,
                    help="Free-form reason / justification (e.g. 'TDD trigger: bug fix')")
    sp.set_defaults(func=cmd_record_skill)

    sp = sub.add_parser("list-skills", help="List all skill activations for a plan + per-skill counts")
    sp.add_argument("plan_id")
    sp.set_defaults(func=cmd_list_skills)

    return p


def main():
    parser = build_parser()
    args = parser.parse_args()
    conn = _open(args)
    try:
        args.func(args, conn)
    except HardStop as e:
        print(f"🛑 HARD STOP: {e}", file=sys.stderr)
        sys.exit(3)
    except CircuitBreakerError as e:
        print(f"🛑 Circuit breaker: {e}", file=sys.stderr)
        sys.exit(2)
    except InvalidTransitionError as e:
        print(f"🛑 Invalid transition: {e}", file=sys.stderr)
        sys.exit(2)
    except (ValueError, sqlite3.IntegrityError) as e:
        print(f"❌ {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
