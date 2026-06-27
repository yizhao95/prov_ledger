"""High-level harness API — `initialize_plan` + `evaluate_and_update_plan`.

Implements the two strictly-typed contracts from Phase 1.docx, plus convenience
helpers that wrap state_machine + circuit_breakers + telemetry.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import string
from datetime import datetime, timezone

from . import circuit_breakers, db, state_machine, telemetry
from .circuit_breakers import HardStop, SoftStop  # noqa: F401  re-export
from .state_machine import InvalidTransitionError, StepStatus  # noqa: F401


def _now_compact() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")


def _step_label(index: int) -> str:
    """0→'A', 1→'B', ..., 25→'Z', 26→'AA', etc."""
    if index < 26:
        return string.ascii_uppercase[index]
    first, second = divmod(index, 26)
    return string.ascii_uppercase[first - 1] + string.ascii_uppercase[second]


# ── Tool 1: initialize_plan ────────────────────────────────────────────
def initialize_plan(
    conn: sqlite3.Connection,
    original_goal: str,
    initial_steps: list[str],
    plan_id_prefix: str = "plan",
    max_revisions: int = 5,
    user_query: str | None = None,
    skills_activated: list[dict] | None = None,
) -> dict:
    """Create a new plan with N top-level steps. Returns {plan_id, step_ids}.

    `original_goal` is the agent's one-line summary of intent.
    `user_query` is the verbatim prompt the human typed (optional, used as
    history-page title).
    `skills_activated` is an optional list of dicts recording which skills were
    already loaded BEFORE the plan was created (e.g., writing-plans + TDD via
    iron-law). Each dict requires keys: skill_name, source. Optional: reason.
    All entries are recorded with step_id=NULL (init-time activation).
    """
    if not initial_steps:
        raise ValueError("initial_steps must contain at least 1 step")
    plan_id = f"{plan_id_prefix}-{_now_compact()}"
    db.insert_plan(conn, plan_id, original_goal, max_revisions=max_revisions, user_query=user_query)
    step_ids = []
    for i, desc in enumerate(initial_steps):
        sid = f"{plan_id}-{_step_label(i)}"
        db.insert_step(
            conn, sid, plan_id, desc,
            execution_order=i, depth_level=0, parent_step_id=None,
        )
        step_ids.append(sid)

    # Record any pre-activated skills (init-time, step_id=NULL)
    if skills_activated:
        for entry in skills_activated:
            db.add_skill_activation(
                conn,
                plan_id=plan_id,
                skill_name=entry["skill_name"],
                source=entry["source"],
                step_id=None,
                reason=entry.get("reason"),
            )

    return {"plan_id": plan_id, "step_ids": step_ids}


# ── Skill activation helper ──────────────────────────────────────────────
def record_skill_activation(
    conn: sqlite3.Connection,
    plan_id: str,
    skill_name: str,
    source: str,
    step_id: str | None = None,
    reason: str | None = None,
) -> int:
    """Record one mid-execution skill activation. Thin wrapper over db.add_skill_activation.

    Use during execution when a step triggers a new skill that wasn't pre-loaded
    at plan-init time (e.g., systematic-debugging triggered by an error in step F,
    or a sub-agent loading a domain skill). For init-time activations, prefer
    passing them via `initialize_plan(skills_activated=[...])`.

    Returns the new activation_id.
    """
    return db.add_skill_activation(
        conn,
        plan_id=plan_id,
        skill_name=skill_name,
        source=source,
        step_id=step_id,
        reason=reason,
    )


# ── Tool 2: evaluate_and_update_plan ──────────────────────────────────────────
def evaluate_and_update_plan(
    conn: sqlite3.Connection,
    deviation_detected: bool,
    target_step_id: str | None = None,
    justification: str | None = None,
    new_sub_steps: list[str] | None = None,
) -> dict:
    """Register a deviation; optionally insert sub-steps. Enforces all 3 circuit breakers."""
    if not deviation_detected:
        return {"accepted": True, "no_changes": True}
    if not target_step_id or not justification:
        raise ValueError("deviation_detected requires both target_step_id AND justification")

    target = db.get_step(conn, target_step_id)
    if not target:
        return {"accepted": False, "reason": f"step_id not found: {target_step_id}"}
    plan = db.get_plan(conn, target["plan_id"])
    if not plan:
        return {"accepted": False, "reason": f"plan_id not found: {target['plan_id']}"}

    # ── Circuit breakers (raise on violation) ────────────────────────────────
    try:
        circuit_breakers.check_immutability(target["status"])
        warning = circuit_breakers.check_loop_prevention(
            plan["revision_count"], plan["max_revisions"]
        )
        if new_sub_steps:
            circuit_breakers.check_depth_limit(target["depth_level"])
    except SoftStop as e:
        return {"accepted": False, "reason": str(e), "breaker": "soft"}
    # HardStop is intentionally NOT caught — it propagates up as agent-pause signal

    new_step_ids: list[str] = []
    existing_children = db.get_children(conn, target_step_id) if new_sub_steps else []
    # BE-C4: sub-step inserts + revision bump + deviation record are one atomic
    # unit — an interruption mid-sequence must not leave the plan half-mutated.
    with db.transaction(conn):
        for i, desc in enumerate(new_sub_steps or []):
            sid = f"{target_step_id}.{len(existing_children) + i + 1}"
            db.insert_step(
                conn, sid, target["plan_id"], desc,
                execution_order=target["execution_order"] * 100 + i,
                parent_step_id=target_step_id,
                depth_level=target["depth_level"] + 1,
                commit=False,
            )
            new_step_ids.append(sid)
        new_revision = db.increment_revision(conn, plan["plan_id"], commit=False)
        deviation_id = db.insert_deviation(
            conn, plan["plan_id"], target_step_id, justification,
            new_step_ids=new_step_ids, revision_count=new_revision, commit=False,
        )
    result: dict = {
        "accepted": True,
        "new_step_ids": new_step_ids,
        "justification_logged": justification,
        "deviation_id": deviation_id,
        "revision_count": new_revision,
    }
    if warning:
        result["warning"] = warning
    return result


# ── Convenience wrappers (validated transitions) ──────────────────────────────
def start_step(conn: sqlite3.Connection, step_id: str) -> dict:
    """PENDING → IN_PROGRESS, sets started_at."""
    step = db.get_step(conn, step_id)
    if not step:
        raise ValueError(f"step_id not found: {step_id}")
    state_machine.validate_transition(step["status"], "IN_PROGRESS")
    db.update_step_status(conn, step_id, "IN_PROGRESS", set_started=True)
    return db.get_step(conn, step_id)


def complete_step(conn: sqlite3.Connection, step_id: str) -> dict:
    """IN_PROGRESS → COMPLETED, sets completed_at."""
    step = db.get_step(conn, step_id)
    if not step:
        raise ValueError(f"step_id not found: {step_id}")
    circuit_breakers.check_immutability(step["status"])  # no double-completing
    state_machine.validate_transition(step["status"], "COMPLETED")
    db.update_step_status(conn, step_id, "COMPLETED", set_completed=True)
    return db.get_step(conn, step_id)


def fail_step(conn: sqlite3.Connection, step_id: str, reason: str = "") -> dict:
    """Fail a started step (STARTING/IN_PROGRESS/NEEDS_REVIEW → FAILED).

    PENDING steps cannot be failed — the state machine rejects PENDING → FAILED
    (start the step first). Persists the reason to Steps.failure_reason and the log.
    """
    step = db.get_step(conn, step_id)
    if not step:
        raise ValueError(f"step_id not found: {step_id}")
    state_machine.validate_transition(step["status"], "FAILED")
    db.update_step_status(conn, step_id, "FAILED", set_completed=True)
    if reason:
        db.set_failure_reason(conn, step_id, reason)
        telemetry.append_step_log(conn, step_id, f"[FAILED] {reason}")
    return db.get_step(conn, step_id)


def append_log(conn: sqlite3.Connection, step_id: str, raw_chunk: str) -> str:
    """Append telemetry to step's log_context (with truncation)."""
    return telemetry.append_step_log(conn, step_id, raw_chunk)


def complete_plan(conn: sqlite3.Connection, plan_id: str) -> dict:
    """Mark plan COMPLETED. Doesn't validate that all steps are done — caller's job."""
    db.update_plan_status(conn, plan_id, "COMPLETED")
    return db.get_plan(conn, plan_id)


# ── Deterministic auto-review-and-complete procedure (migration 006) ───────────────
TERMINAL_STEP_STATES = {"COMPLETED", "FAILED"}


def _is_step_recovered(conn: sqlite3.Connection, step_id: str) -> bool:
    """True if the step's outcome should be treated as success for plan-level rollup.

    Rules:
      - COMPLETED                           → recovered (trivially)
      - FAILED with zero non-review children → NOT recovered (failure stuck)
      - FAILED with all non-review children recovered (recursive) → recovered
      - FAILED with at least one unrecovered/non-terminal child → NOT recovered
      - Any non-terminal state (PENDING / IN_PROGRESS / STARTING) → NOT recovered

    Recursion is naturally bounded by the depth_level<=3 circuit breaker in
    executing-plans, so this function will not pathologically deep-recurse.
    """
    row = conn.execute(
        "SELECT status FROM Steps WHERE step_id = ?", (step_id,)
    ).fetchone()
    if row is None:
        return False
    status = row["status"]
    if status == "COMPLETED":
        return True
    if status != "FAILED":
        # PENDING / IN_PROGRESS / STARTING — not terminal, cannot be 'recovered'
        return False
    # FAILED: check deviation sub-tree
    children = conn.execute(
        "SELECT step_id FROM Steps WHERE parent_step_id = ? AND is_review = 0",
        (step_id,),
    ).fetchall()
    if not children:
        return False  # FAILED leaf — no deviation, not recovered
    return all(_is_step_recovered(conn, c["step_id"]) for c in children)


DEFAULT_REGISTRY_PATH = os.path.expanduser(
    "~/skill-workspace/project-graphs/projects.json"
)

# Sentinel so callers can pass registry_path=None to mean "use the default";
# the default itself honors the PSG_REGISTRY_PATH env override (test isolation +
# parity with project-state-graph's env-overridable registry paths).
_REGISTRY_DEFAULT = object()


def _resolve_registry_path(registry_path) -> str:
    if registry_path is _REGISTRY_DEFAULT or registry_path is None:
        return os.environ.get("PSG_REGISTRY_PATH", DEFAULT_REGISTRY_PATH)
    return registry_path


def _normalize_project_token(s: str) -> str:
    """Canonicalize a project name / text for variance-tolerant matching.

    Lowercase and strip all hyphens, underscores, and whitespace so that
    'demo-app', 'demo app', 'demo_app', 'DEMOAPP' all collapse to
    the same token 'demoapp'.
    """
    return re.sub(r"[-_\s]+", "", s.lower())


def detect_registered_project(
    conn: sqlite3.Connection,
    plan_id: str,
    registry_path=_REGISTRY_DEFAULT,
) -> str | None:
    """Return the canonical name of a registered project mentioned by the plan.

    Scans the plan goal + every NON-review step description for a mention of any
    project in the registry (projects.json), using variance-tolerant matching
    (see _normalize_project_token). Returns the first registered project's
    canonical name found, or None if no registered project is mentioned (or the
    registry is empty/absent).

    Deterministic — no LLM. Used by review_and_complete to decide whether plan
    completion needs an LLM sub-agent review.
    """
    registry_path = _resolve_registry_path(registry_path)
    if not registry_path or not os.path.exists(registry_path):
        return None
    try:
        with open(registry_path) as f:
            registry = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None
    projects = registry.get("projects", []) if isinstance(registry, dict) else []
    if not projects:
        return None

    # Build {normalized_name: canonical_name}, preserving registry order.
    normalized = []
    for p in projects:
        name = p.get("name")
        if name:
            normalized.append((_normalize_project_token(name), name))
    if not normalized:
        return None

    # Gather plan text: goal + non-review step descriptions.
    plan = db.get_plan(conn, plan_id)
    texts = []
    if plan and plan.get("original_goal"):
        texts.append(plan["original_goal"])
    step_rows = conn.execute(
        "SELECT description FROM Steps "
        "WHERE plan_id = ? AND is_review = 0 AND description IS NOT NULL",
        (plan_id,),
    ).fetchall()
    texts.extend(r["description"] for r in step_rows)

    # BE-S1: match on word boundaries, not raw substring. Splitting into word
    # tokens (then matching a project's collapsed name against a CONTIGUOUS RUN of
    # tokens) keeps variance tolerance — "demo app" / "demo-app" / "demoapp" all
    # match "demo-app" — while preventing a short name like "app" from matching
    # inside an unrelated word like "happens".
    hay_words = [w for w in re.split(r"[-_\s]+", " ".join(texts).lower()) if w]
    for norm_name, canonical in normalized:
        if norm_name and _matches_token_run(hay_words, norm_name):
            return canonical
    return None


def _matches_token_run(words: list[str], target: str) -> bool:
    """True if `target` equals the concatenation of some contiguous run of `words`.

    `target` is an already-collapsed project name (no separators). A single-word
    project matches a standalone token; a multi-word project matches adjacent
    tokens whose concatenation equals it.
    """
    n = len(words)
    for i in range(n):
        acc = ""
        for j in range(i, n):
            acc += words[j]
            if len(acc) > len(target):
                break
            if acc == target:
                return True
    return False


def _open_agent_review(conn: sqlite3.Connection, plan_id: str, review_step_id: str) -> str:
    """Enter the tracked agent-review phase for a registered-project plan.

    Deterministically:
      - flip the review step to NEEDS_REVIEW,
      - leave the plan IN_PROGRESS,
      - bump the plan revision (this IS a plan revision — we add a step),
      - insert a child SUB_AGENT step <review>.1 (PENDING, depth 1) that the
        review sub-agent will drive.

    Idempotent: if a child step already exists under the review step, do NOT
    re-insert it and do NOT bump the revision again. Returns the child step_id.
    """
    existing = db.get_children(conn, review_step_id)
    if existing:
        # Already opened — return the first child (there is only ever one).
        return existing[0]["step_id"]

    review_row = db.get_step(conn, review_step_id)
    child_id = f"{review_step_id}.1"
    # BE-C4: the review-step flip + plan-state writes + child insert are one
    # atomic unit; a crash mid-sequence must not park a plan with no child step.
    with db.transaction(conn):
        db.update_step_status(conn, review_step_id, "NEEDS_REVIEW", commit=False)
        db.update_plan_status(conn, plan_id, "IN_PROGRESS", commit=False)
        db.set_review_state(conn, plan_id, "awaiting_agent", commit=False)  # BE-D4
        db.increment_revision(conn, plan_id, commit=False)
        db.insert_step(
            conn,
            child_id,
            plan_id,
            "AGENT REVIEW: project-state-graph consistency review (LLM sub-agent)",
            execution_order=(review_row["execution_order"] or 0) * 100,
            parent_step_id=review_step_id,
            depth_level=(review_row["depth_level"] or 0) + 1,
            step_type="SUB_AGENT",
            commit=False,
        )
    return child_id


def review_and_complete(
    conn: sqlite3.Connection,
    plan_id: str,
    registry_path=_REGISTRY_DEFAULT,
) -> dict:
    """Deterministic 'review and complete' procedure.

    Examines every non-review step in plan_id and decides the plan's terminal
    state purely from data:

      - all non-review steps COMPLETED         → review COMPLETED, plan COMPLETED
      - any non-review step FAILED but the failure is RECOVERED via a deviation
        sub-tree (see _is_step_recovered)      → review COMPLETED, plan COMPLETED
      - any non-review step FAILED & UNRECOVERED (no children, or some child
        also failed without its own recovery)  → review FAILED, plan FAILED
        (provided all other non-review steps are in some terminal state)
      - any non-review step still PENDING
        or IN_PROGRESS                         → no-op, returns ready=False

    LLM-review routing (project-state-graph): when the plan WOULD close as
    COMPLETED *and* it mentions a registered project (detect_registered_project),
    the review step is instead flipped to NEEDS_REVIEW and the plan is LEFT
    IN_PROGRESS. The result carries needs_agent_review=True + project so the
    executing-plans main agent knows to dispatch the review sub-agent, which
    finalizes the plan via agent-review-close.sh. The FAILED path is never
    intercepted — unrecovered failures always propagate immediately.

    Idempotent: re-calling when the review step is already terminal OR already
    NEEDS_REVIEW returns the current state without re-mutating.
    """
    # Find the review step (at most one per plan; enforced by step_id uniqueness)
    review_row = conn.execute(
        "SELECT step_id, status FROM Steps WHERE plan_id = ? AND is_review = 1 LIMIT 1",
        (plan_id,),
    ).fetchone()
    if review_row is None:
        return {
            "ready": False,
            "plan_status": (db.get_plan(conn, plan_id) or {}).get("status"),
            "review_step_id": None,
            "review_status": None,
            "reason": "no review step (pre-migration-006 plan?)",
        }

    review_step_id = review_row["step_id"]
    review_status = review_row["status"]

    # Idempotency: if review step already finalized, return current state
    if review_status in TERMINAL_STEP_STATES:
        plan_row = db.get_plan(conn, plan_id)
        return {
            "ready": True,
            "plan_status": plan_row["status"] if plan_row else None,
            "review_step_id": review_step_id,
            "review_status": review_status,
            "reason": "review step already terminal (idempotent no-op)",
        }

    # Agent-review phase: if the review step is awaiting an LLM sub-agent, the
    # plan's terminal state is driven by the tracked child step <review>.1.
    # This MUST run before the sibling tally below, because the child step has
    # is_review=0 and would otherwise be miscounted as a regular sibling.
    if review_status == "NEEDS_REVIEW":
        children = db.get_children(conn, review_step_id)
        child = children[0] if children else None
        if child is None:
            # Heal-forward: a plan parked in NEEDS_REVIEW by older code with no
            # child step. Open the tracked review now (idempotent).
            child_id = _open_agent_review(conn, plan_id, review_step_id)
            return {
                "ready": False,
                "needs_agent_review": True,
                "project": detect_registered_project(conn, plan_id, registry_path),
                "plan_status": "IN_PROGRESS",
                "review_step_id": review_step_id,
                "review_child_step_id": child_id,
                "review_status": "NEEDS_REVIEW",
                "reason": "review awaiting agent; child review step (re)created",
            }
        if child["status"] == "COMPLETED":
            db.update_step_status(conn, review_step_id, "COMPLETED", set_completed=True)
            db.update_plan_status(conn, plan_id, "COMPLETED")
            db.set_review_state(conn, plan_id, "reviewed")  # BE-D4
            return {
                "ready": True,
                "plan_status": "COMPLETED",
                "review_step_id": review_step_id,
                "review_status": "COMPLETED",
                "review_child_step_id": child["step_id"],
                "reason": "agent review child step COMPLETED; finalizing plan COMPLETED",
            }
        if child["status"] == "FAILED":
            db.update_step_status(conn, review_step_id, "FAILED", set_completed=True)
            db.update_plan_status(conn, plan_id, "FAILED")
            db.set_review_state(conn, plan_id, "reviewed")  # BE-D4
            return {
                "ready": True,
                "plan_status": "FAILED",
                "review_step_id": review_step_id,
                "review_status": "FAILED",
                "review_child_step_id": child["step_id"],
                "reason": "agent review child step FAILED; propagating FAILED to plan",
            }
        # child still PENDING / IN_PROGRESS — keep waiting (idempotent no-op)
        return {
            "ready": False,
            "needs_agent_review": True,
            "project": detect_registered_project(conn, plan_id, registry_path),
            "plan_status": "IN_PROGRESS",
            "review_step_id": review_step_id,
            "review_child_step_id": child["step_id"],
            "review_status": "NEEDS_REVIEW",
            "reason": "review step NEEDS_REVIEW; awaiting agent child step outcome",
        }

    # Tally sibling (non-review) step statuses
    tally_rows = conn.execute(
        "SELECT status, COUNT(*) AS n FROM Steps "
        "WHERE plan_id = ? AND is_review = 0 GROUP BY status",
        (plan_id,),
    ).fetchall()
    tally = {row["status"]: row["n"] for row in tally_rows}
    pending = tally.get("PENDING", 0) + tally.get("STARTING", 0)
    in_progress = tally.get("IN_PROGRESS", 0)
    failed = tally.get("FAILED", 0)
    completed = tally.get("COMPLETED", 0)
    total_non_review = pending + in_progress + failed + completed

    # Decision tree
    if pending > 0 or in_progress > 0:
        return {
            "ready": False,
            "plan_status": "IN_PROGRESS",
            "review_step_id": review_step_id,
            "review_status": "PENDING",
            "reason": (
                f"{pending} pending + {in_progress} in_progress step(s) remaining; "
                f"review deferred"
            ),
        }

    if total_non_review == 0:
        # Plan has zero non-review steps (weird edge case — publish-plan rejects
        # empty steps lists). Treat as COMPLETED for safety.
        new_plan_status = "COMPLETED"
        new_review_status = "COMPLETED"
        reason = "plan has zero non-review steps; trivially complete"
    elif failed > 0:
        # New recovery-aware logic (Jun 2026): a FAILED step is 'recovered' if
        # its deviation sub-tree resolves successfully. Only UNRECOVERED FAILED
        # steps poison the plan.
        failed_step_ids = [
            r["step_id"] for r in conn.execute(
                "SELECT step_id FROM Steps "
                "WHERE plan_id = ? AND is_review = 0 AND status = 'FAILED'",
                (plan_id,),
            ).fetchall()
        ]
        # Only consider TOP-LEVEL failed steps (parent_step_id IS NULL or parent
        # is itself non-FAILED) — children of a FAILED parent are already
        # accounted for by the recursive _is_step_recovered walk and would
        # otherwise double-count.
        top_failed = []
        for sid in failed_step_ids:
            parent = conn.execute(
                "SELECT parent_step_id FROM Steps WHERE step_id = ?", (sid,)
            ).fetchone()
            parent_id = parent["parent_step_id"] if parent else None
            if parent_id is None:
                top_failed.append(sid)
            else:
                # If parent is itself FAILED, this child is part of the parent's
                # recovery sub-tree — don't count it independently.
                p_row = conn.execute(
                    "SELECT status FROM Steps WHERE step_id = ?", (parent_id,)
                ).fetchone()
                if not p_row or p_row["status"] != "FAILED":
                    top_failed.append(sid)
        unrecovered = [sid for sid in top_failed if not _is_step_recovered(conn, sid)]
        if unrecovered:
            new_plan_status = "FAILED"
            new_review_status = "FAILED"
            sample = ", ".join(unrecovered[:3])
            more = f" (+{len(unrecovered)-3} more)" if len(unrecovered) > 3 else ""
            reason = (
                f"{len(unrecovered)} unrecovered failed step(s) [{sample}{more}]; "
                f"propagating FAILED to plan"
            )
        else:
            new_plan_status = "COMPLETED"
            new_review_status = "COMPLETED"
            reason = (
                f"all {len(top_failed)} failure(s) recovered via deviation "
                f"sub-tree(s); finalizing plan as COMPLETED"
            )
    else:
        # All non-review steps COMPLETED
        new_plan_status = "COMPLETED"
        new_review_status = "COMPLETED"
        reason = f"all {completed} non-review step(s) COMPLETED; finalizing plan"

    # LLM-review routing: when the plan WOULD close COMPLETED and it mentions a
    # registered project, defer the close to an agent review instead. The FAILED
    # path is never intercepted — unrecovered failures propagate immediately.
    if new_plan_status == "COMPLETED":
        project = detect_registered_project(conn, plan_id, registry_path)
        if project is not None:
            child_id = _open_agent_review(conn, plan_id, review_step_id)
            return {
                "ready": False,
                "needs_agent_review": True,
                "project": project,
                "plan_status": "IN_PROGRESS",
                "review_step_id": review_step_id,
                "review_child_step_id": child_id,
                "review_status": "NEEDS_REVIEW",
                "reason": (
                    f"plan mentions registered project '{project}'; deferring "
                    f"completion to LLM sub-agent review (NEEDS_REVIEW), child "
                    f"step {child_id} created"
                ),
            }

    # Apply writes: review step status first, then plan status.
    # Use db.update_step_status with set_completed=True so completed_at gets set.
    db.update_step_status(conn, review_step_id, new_review_status, set_completed=True)
    db.update_plan_status(conn, plan_id, new_plan_status)

    return {
        "ready": True,
        "plan_status": new_plan_status,
        "review_step_id": review_step_id,
        "review_status": new_review_status,
        "reason": reason,
    }
