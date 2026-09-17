"""High-level harness API — `initialize_plan` + `evaluate_and_update_plan`.

Implements the two strictly-typed contracts from Phase 1.docx, plus convenience
helpers that wrap state_machine + circuit_breakers + telemetry.
"""
from __future__ import annotations

import json
import subprocess
import os
import re
import sqlite3
import string
from datetime import datetime, timezone

from . import significance, checks, circuit_breakers, constraints, db, outcomes, psg_bridge, reasons, state_machine, telemetry, triggers
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
def _step_spec(spec) -> tuple[str, str | None]:
    """Normalize a step spec — a bare description string, or a dict of
    {description, step_type?} — to (description, step_type).
    step_type validity is enforced by db.insert_step."""
    if isinstance(spec, str):
        return spec, None
    return spec["description"], spec.get("step_type")


def initialize_plan(
    conn: sqlite3.Connection,
    original_goal: str,
    initial_steps: list,
    plan_id_prefix: str = "plan",
    max_revisions: int = 5,
    user_query: str | None = None,
    skills_activated: list[dict] | None = None,
    project: str | None = None,
    project_source: str | None = None,
) -> dict:
    """Create a new plan with N top-level steps. Returns {plan_id, step_ids}.

    `project` / `project_source` (FL-014) record which registered project the
    plan belongs to and how that was decided ('declared' | 'cwd'); "none" is
    the explicit "no project". Left NULL, review_and_complete falls back to a
    one-time legacy goal-text match.

    `original_goal` is the agent's one-line summary of intent.
    `initial_steps` items are bare description strings, or dicts of
    {description, step_type?} so a type declared at plan time lands at publish
    (previously it was silently dropped; run-step may still set/override the
    type at execution time).
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
    # DP phase 2b (Task 4b): the id has one-second resolution; a second plan in the
    # same second (a suite under load, a script) takes a -2, -3 … suffix instead of
    # failing on UNIQUE
    base, n = plan_id, 1
    while conn.execute("SELECT 1 FROM Plans WHERE plan_id = ?", (plan_id,)).fetchone():
        n += 1
        plan_id = f"{base}-{n}"
    db.insert_plan(conn, plan_id, original_goal, max_revisions=max_revisions, user_query=user_query,
                   project=project, project_source=project_source)
    step_ids = []
    for i, spec in enumerate(initial_steps):
        desc, step_type = _step_spec(spec)
        sid = f"{plan_id}-{_step_label(i)}"
        db.insert_step(
            conn, sid, plan_id, desc,
            execution_order=i, depth_level=0, parent_step_id=None,
            step_type=step_type,
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
    new_sub_steps: list | None = None,
) -> dict:
    """Register a deviation; optionally insert sub-steps. Enforces all 3 circuit
    breakers. Sub-step items are description strings or {description, step_type?}
    dicts (same shapes as initialize_plan's initial_steps)."""
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
        for i, spec in enumerate(new_sub_steps or []):
            desc, step_type = _step_spec(spec)
            sid = f"{target_step_id}.{len(existing_children) + i + 1}"
            db.insert_step(
                conn, sid, target["plan_id"], desc,
                execution_order=target["execution_order"] * 100 + i,
                parent_step_id=target_step_id,
                depth_level=target["depth_level"] + 1,
                step_type=step_type,
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


def complete_step(conn: sqlite3.Connection, step_id: str, commit: bool = True) -> dict:
    """IN_PROGRESS → COMPLETED, sets completed_at. commit=False joins an
    enclosing db.transaction (FL-017: status + log land together)."""
    step = db.get_step(conn, step_id)
    if not step:
        raise ValueError(f"step_id not found: {step_id}")
    circuit_breakers.check_immutability(step["status"])  # no double-completing
    state_machine.validate_transition(step["status"], "COMPLETED")
    db.update_step_status(conn, step_id, "COMPLETED", set_completed=True, commit=commit)
    return db.get_step(conn, step_id)


def fail_step(conn: sqlite3.Connection, step_id: str, reason: str = "", commit: bool = True) -> dict:
    """Fail a started step (STARTING/IN_PROGRESS/NEEDS_REVIEW → FAILED).

    PENDING steps cannot be failed — the state machine rejects PENDING → FAILED
    (start the step first). Persists the reason to Steps.failure_reason and the
    log; commit=False joins an enclosing db.transaction.
    """
    step = db.get_step(conn, step_id)
    if not step:
        raise ValueError(f"step_id not found: {step_id}")
    state_machine.validate_transition(step["status"], "FAILED")
    db.update_step_status(conn, step_id, "FAILED", set_completed=True, commit=commit)
    if reason:
        db.set_failure_reason(conn, step_id, reason, commit=commit)
        telemetry.append_step_log(conn, step_id, f"[FAILED] {reason}", commit=commit)
    return db.get_step(conn, step_id)


def append_log(conn: sqlite3.Connection, step_id: str, raw_chunk: str, commit: bool = True) -> str:
    """Append telemetry to step's log_context (with truncation)."""
    return telemetry.append_step_log(conn, step_id, raw_chunk, commit=commit)


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


NO_PROJECT_MENTIONED = "no registered project mentioned in goal/steps (FL-014: token match)"


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
    completion needs an LLM sub-agent review. See _detect_with_reason for the
    WHY when the answer is None (S1: a skipped review is never silent).
    """
    return _detect_with_reason(conn, plan_id, registry_path)[0]


def _project_for_review(
    conn: sqlite3.Connection,
    plan_id: str,
    registry_path=_REGISTRY_DEFAULT,
) -> tuple[str | None, str, str | None]:
    """(project, why, source) — FL-014: the plan's project comes from
    Plans.project (declared at publish, derived from the cwd repo, or "none");
    the goal-text token match is a compatibility path for NULL rows only and,
    when it hits, the result is stored once as 'legacy'."""
    plan = db.get_plan(conn, plan_id) or {}
    stored, source = plan.get("project"), plan.get("project_source")
    if stored == "none":
        return None, "plan declared project=none", source
    if stored:
        path = _resolve_registry_path(registry_path)
        names: set[str] = set()
        if path and os.path.exists(path):
            try:
                with open(path) as f:
                    names = {p.get("name") for p in json.load(f).get("projects", [])}
            except (json.JSONDecodeError, OSError):
                names = set()
        if stored in names:
            return stored, source or "declared", source
        return None, f"project {stored!r} not registered", source
    project, why = _detect_with_reason(conn, plan_id, registry_path)
    if project is not None:
        db.set_plan_project(conn, plan_id, project, "legacy")
        return project, "legacy token match", "legacy"
    return None, why, None


def _detect_with_reason(
    conn: sqlite3.Connection,
    plan_id: str,
    registry_path=_REGISTRY_DEFAULT,
) -> tuple[str | None, str]:
    """(project, why): the registered project the plan mentions, or None plus
    the reason — 'registry not found at …', 'registry unreadable …',
    'registry has no projects', or NO_PROJECT_MENTIONED."""
    registry_path = _resolve_registry_path(registry_path)
    if not registry_path or not os.path.exists(registry_path):
        return None, f"registry not found at {registry_path}"
    try:
        with open(registry_path) as f:
            registry = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        return None, f"registry unreadable at {registry_path}: {e}"
    projects = registry.get("projects", []) if isinstance(registry, dict) else []
    if not projects:
        return None, "registry has no projects"

    # Build {normalized_name: canonical_name}, preserving registry order.
    normalized = []
    for p in projects:
        name = p.get("name")
        if name:
            normalized.append((_normalize_project_token(name), name))
    if not normalized:
        return None, "registry has no projects"

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
            return canonical, "mentioned"
    return None, NO_PROJECT_MENTIONED


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


def _regular_failures_recovered(conn: sqlite3.Connection, plan_id: str) -> bool:
    """True when the plan has no non-terminal regular step and every top-level
    FAILED regular step is recovered (FL-022 re-judge precondition)."""
    rows = conn.execute(
        "SELECT step_id, status, parent_step_id FROM Steps WHERE plan_id = ? AND is_review = 0", (plan_id,)
    ).fetchall()
    if any(r["status"] in ("PENDING", "STARTING", "IN_PROGRESS") for r in rows):
        return False
    failed = [r for r in rows if r["status"] == "FAILED"]
    if not failed:
        return False          # nothing to re-judge: the plan did not fail through a regular step
    by_id = {r["step_id"]: r for r in rows}
    top = [r for r in failed if r["parent_step_id"] is None or by_id.get(r["parent_step_id"], {}).get("status") != "FAILED"]
    return all(_is_step_recovered(conn, r["step_id"]) for r in top)


def _close_reviewed(conn: sqlite3.Connection, plan_id: str, review_step_id: str, child,
                    registry_path, reopened: bool = False) -> dict:
    """Finalize a reviewed plan as COMPLETED — the close-time capture of spec
    §2.8/§2.9 (unstated backstop, rejected paths, constraint bypasses) and the
    terminal writes in ONE transaction; nothing here can block the close.
    `reopened` (FL-019): the review had FAILED and its child recovered."""
    project, _why, project_source = _project_for_review(conn, plan_id, registry_path)
    psg_db = _usable_graph(project, registry_path)
    if reopened and project:
        # FL-030: a recovery sub-step completing is not evidence that the graph
        # was refreshed. Refuse to close while the registry is behind HEAD, and
        # put the reason-slot checklist on the record before the backstop.
        reg_path = _resolve_registry_path(registry_path)
        registered = psg_bridge.registered_sha_for(project, reg_path)
        repo = psg_bridge.repo_for(project, reg_path)
        head = _git_head(repo)
        if registered and head and registered != head:
            telemetry.append_step_log(
                conn, review_step_id,
                f"[REVIEW REOPENED] registry behind HEAD ({registered[:10]} != {head[:10]}): refresh first "
                "(review_run.py --as-recovery <sub-step>) — not closing (FL-030)")
            plan_row = db.get_plan(conn, plan_id) or {}
            return {
                "ready": False,
                "needs_agent_review": True,
                "project": project,
                "project_source": project_source,
                "plan_status": plan_row.get("status"),
                "review_step_id": review_step_id,
                "review_child_step_id": child["step_id"],
                "review_status": "FAILED",
                "reopened": True,
                "reason": "registry behind HEAD: refresh (review_run.py --as-recovery) before closing",
            }
        if not (registered and head):
            telemetry.append_step_log(
                conn, review_step_id,
                f"[REVIEW REOPENED] sha check skipped: registry sha={registered!r}, repo HEAD={head!r} "
                f"(repo {repo!r}) — cannot verify the refresh (FL-030)")
        slots = reasons.slots_for_plan(conn, project, plan_id, psg_db) if psg_db else []
        telemetry.append_step_log(
            conn, review_step_id,
            f"[REASON SLOTS] {len(slots)} open before the reopened close"
            + (": " + reasons.checklist_text(slots) if slots else
               ("" if psg_db else " (state graph unavailable)")))
    close_mode = _close_mode(project, registry_path)
    with db.transaction(conn):
        # DP phase 1 (Task 5): the deterministic rules first — a node a rule
        # recognises gets a derived reason and is never asked about (C1); the
        # rest of the changed nodes are logged as ask / silent (C2, C4).
        triggered = triggers.evaluate(conn, project=project, plan_id=plan_id, psg_db_path=psg_db,
                                      ask=(close_mode != "pending"),
                                      external_mode=_external_trigger_mode(project, registry_path),
                                      commit=False) if psg_db else {}
        n_unstated = reasons.backstop_unstated(
            conn, project=project, plan_id=plan_id, psg_db_path=psg_db, commit=False,
            state="unknown" if close_mode == "pending" else "active") if psg_db else 0
        # DP phase 2b (Task 2): every new reason of the plan gets its significance hint
        # (one significance_log row each); reasons.significance: llm asks for a logged
        # verdict too — the default is hint, zero model calls
        sig_mode = _significance_mode(project, registry_path)
        significance_close = significance.apply_for_plan(conn, project=project, plan_id=plan_id, psg_db_path=psg_db,
                                                         mode=sig_mode, commit=False) if project else {}
        # DP phase 2 (Task 3): blocking findings proceeded past or never answered
        # become survival expectations, so going past them has an outcome
        headline_close = checks.close_headline(conn, plan_id=plan_id, commit=False) if project else {}
        n_rejected = reasons.rejected_paths(
            conn, project=project, plan_id=plan_id, psg_db_path=psg_db, commit=False) if psg_db else 0
        n_bypassed = constraints.bypassed_at_close(
            conn, project=project, plan_id=plan_id, psg_db_path=psg_db,
            review_step_id=review_step_id, commit=False) if psg_db else 0
        db.update_step_status(conn, review_step_id, "COMPLETED", set_completed=True, commit=False)
        db.update_plan_status(conn, plan_id, "COMPLETED", commit=False)
        db.set_review_state(conn, plan_id, "reviewed", commit=False)  # BE-D4
        if reopened:
            telemetry.append_step_log(
                conn, review_step_id,
                f"[REVIEW REOPENED] child {child['step_id']} FAILED but its deviation sub-tree recovered; "
                "closing COMPLETED (FL-019)", commit=False)
    if project and psg_db is None:
        telemetry.append_step_log(
            conn, review_step_id,
            f"[REASONS] state graph unavailable for project {project!r} — no reason slots generated")
    # Spec §3.5 (path C): the close of a registered-project plan backfills the
    # OTHER plans' pending expectations — observed / survival / none_available.
    # Its own transaction; a failure here is logged, never a blocked close.
    backfilled: dict = {}
    if project:
        try:
            backfilled = outcomes.backfill(conn, project, psg_db, plan_id)
            if any(backfilled.get(k) for k in ("observed", "survival", "none_available")):
                telemetry.append_step_log(conn, review_step_id, f"[OUTCOMES] backfilled {backfilled}")
        except Exception as exc:  # pragma: no cover - defensive
            telemetry.append_step_log(conn, review_step_id, f"[OUTCOMES] backfill failed: {exc}")
            backfilled = {"error": str(exc)}
    # DP phase 3 (Task 1, spec §7): the ledger's chain heads leave the database
    # and become a git note on HEAD, so the record of "these rows existed here"
    # is no longer kept by the thing being checked. Its own try/except: a repo
    # without git, without a commit, or with a refusing notes ref is a logged
    # warning, never a plan that will not close.
    anchor = _anchor_close(conn, project, plan_id, review_step_id, registry_path)
    return {
        "ready": True,
        "plan_status": "COMPLETED",
        "review_step_id": review_step_id,
        "review_status": "COMPLETED",
        "review_child_step_id": child["step_id"],
        "reason": ("agent review child step recovered after FAILED; finalizing plan COMPLETED" if reopened
                   else "agent review child step COMPLETED; finalizing plan COMPLETED"),
        "reopened": reopened,
        "project": project,
        "project_source": project_source,
        "unstated_backstopped": n_unstated,
        "close_mode": close_mode,
        "triggers": triggered,
        "headline": headline_close,
        "significance": significance_close,
        "rejected_paths": n_rejected,
        "constraints_bypassed": n_bypassed,
        "outcomes_backfilled": backfilled,
        "anchor": anchor,
    }


def _anchor_mode(project: str | None, registry_path) -> str:
    """provledger-extensions.json → integrity.anchor (on | off) of the project's
    repo. `on` whenever in doubt: anchoring is the default, turning it off is a
    choice somebody has to write down."""
    if not project:
        return "on"
    try:
        from . import extensions
        repo = psg_bridge.repo_for(project, _resolve_registry_path(registry_path))
        path = extensions.discover(repo) if repo else None
        return extensions.load(path).integrity_anchor if path else "on"
    except Exception:
        return "on"


def _anchor_close(conn: sqlite3.Connection, project: str | None, plan_id: str,
                  review_step_id: str, registry_path) -> dict:
    """Append the three chain heads to `git notes --ref provledger` on the repo's
    HEAD. Every outcome — written, switched off, or failed — leaves one
    `[ANCHOR]` line on the review step, because an anchor that quietly did not
    happen is exactly the kind of silence this product exists to remove."""
    from . import integrity
    if not project:
        return {"anchored": False, "mode": "on", "reason": "the plan belongs to no registered project"}
    mode = _anchor_mode(project, registry_path)
    if mode == "off":
        telemetry.append_step_log(
            conn, review_step_id,
            f"[ANCHOR] off: provledger-extensions.json says integrity.anchor=off, so the chain heads of {plan_id} "
            f"were NOT written to refs/notes/{integrity.NOTES_REF}")
        return {"anchored": False, "mode": "off", "reason": "integrity.anchor=off"}
    repo = psg_bridge.repo_for(project, _resolve_registry_path(registry_path))
    try:
        if not repo:
            raise integrity.AnchorError(f"no repo registered for project {project!r}")
        payload = integrity.anchor_payload(conn, plan_id=plan_id)
        note = integrity.anchor_heads(repo, payload)
        commit = integrity.head_commit(repo)
        heads = ", ".join(f"{t} #{payload[t]['id']} {(payload[t]['hash'] or '')[:12]}" for t in integrity.CHAINS)
        telemetry.append_step_log(
            conn, review_step_id,
            f"[ANCHOR] chain heads anchored in refs/notes/{integrity.NOTES_REF}: note {note[:12]} @ {commit[:12]} "
            f"· {heads} · push it with `git push origin refs/notes/{integrity.NOTES_REF}`")
        return {"anchored": True, "mode": "on", "note_sha": note, "commit": commit,
                "at": payload["at"], "ref": integrity.NOTES_REF,
                "heads": {t: payload[t] for t in integrity.CHAINS}}
    except integrity.AnchorError as exc:
        if "nothing to anchor" in str(exc):
            telemetry.append_step_log(
                conn, review_step_id,
                f"[ANCHOR] nothing to anchor: {exc} — {plan_id} closed with no note, because a note full of "
                "nulls witnesses nothing.")
            return {"anchored": False, "mode": mode, "reason": str(exc), "empty": True}
        telemetry.append_step_log(
            conn, review_step_id,
            f"[ANCHOR] not anchored: {exc} — {plan_id} closed anyway (spec §7: a notes failure is a warning, "
            "never a block). The ledger still verifies; it just has no outside witness for this close.")
        return {"anchored": False, "mode": mode, "reason": str(exc)}
    except Exception as exc:  # pragma: no cover - defensive
        telemetry.append_step_log(
            conn, review_step_id,
            f"[ANCHOR] not anchored: {exc} — {plan_id} closed anyway (spec §7: a notes failure is a warning, "
            "never a block). The ledger still verifies; it just has no outside witness for this close.")
        return {"anchored": False, "mode": mode, "reason": str(exc)}


def _external_trigger_mode(project: str | None, registry_path) -> str:
    """provledger-extensions.json → reasons.external_trigger (off | on); off
    whenever in doubt. The judge may not ask anyone anything until a gate report
    says it is calibrated, and an unreadable extensions file is doubt."""
    if not project:
        return "off"
    try:
        from . import extensions
        repo = psg_bridge.repo_for(project, _resolve_registry_path(registry_path))
        path = extensions.discover(repo) if repo else None
        return extensions.load(path).reasons_external_trigger if path else "off"
    except Exception:
        return "off"


def _significance_mode(project: str | None, registry_path) -> str:
    """provledger-extensions.json → reasons.significance (hint | llm); hint whenever in doubt."""
    if not project:
        return "hint"
    try:
        from . import extensions
        repo = psg_bridge.repo_for(project, _resolve_registry_path(registry_path))
        path = extensions.discover(repo) if repo else None
        return extensions.load(path).reasons_significance if path else "hint"
    except Exception:
        return "hint"


def _close_mode(project: str | None, registry_path) -> str:
    """provledger-extensions.json → reasons.close_mode (ask | pending) of the project's repo."""
    if not project:
        return "ask"
    try:
        from . import extensions
        repo = psg_bridge.repo_for(project, _resolve_registry_path(registry_path))
        path = extensions.discover(repo) if repo else None
        return extensions.load(path).reasons_close_mode if path else "ask"
    except Exception:
        return "ask"


def _git_head(repo: str | None) -> str | None:
    """HEAD of a git checkout, None when there is none to ask."""
    if not repo or not os.path.isdir(repo):
        return None
    try:
        r = subprocess.run(["git", "-C", repo, "rev-parse", "HEAD"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout.strip() or None if r.returncode == 0 else None


def _usable_graph(project: str | None, registry_path) -> str | None:
    """db_path of the project's state graph when it is registered AND present
    on disk; None otherwise (callers log 'state graph unavailable')."""
    if not project:
        return None
    path = psg_bridge.db_path_for(project, _resolve_registry_path(registry_path))
    return path if path and os.path.exists(path) else None


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

    # Idempotency: if review step already finalized, return current state —
    # EXCEPT (FL-019) a FAILED review whose child REVIEW.1 has since been
    # recovered through a deviation sub-tree: that is a re-open, not a no-op.
    if review_status in TERMINAL_STEP_STATES:
        if review_status == "FAILED":
            child = next(iter(db.get_children(conn, review_step_id)), None)
            if child is not None and child["status"] == "FAILED" and _is_step_recovered(conn, child["step_id"]):
                return _close_reviewed(conn, plan_id, review_step_id, child, registry_path, reopened=True)
            if child is None and _regular_failures_recovered(conn, plan_id):
                # FL-022: the plan failed through a regular step (no review
                # child was ever created); every failed step has since been
                # recovered through a deviation sub-tree. Re-judge once: the
                # host resets the review row (FAILED -> PENDING is not a step
                # transition the state machine offers, like FL-019's re-open)
                # and runs the normal decision below.
                with db.transaction(conn):
                    db.update_step_status(conn, review_step_id, "PENDING", commit=False)
                    db.update_plan_status(conn, plan_id, "IN_PROGRESS", commit=False)
                    telemetry.append_step_log(
                        conn, review_step_id,
                        "[REVIEW REOPENED] plan had failed through regular step(s); all recovered — re-judging (FL-022)",
                        commit=False)
                out = review_and_complete(conn, plan_id, registry_path)
                out["reopened"] = True
                return out
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
                "project": _project_for_review(conn, plan_id, registry_path)[0],
                "plan_status": "IN_PROGRESS",
                "review_step_id": review_step_id,
                "review_child_step_id": child_id,
                "review_status": "NEEDS_REVIEW",
                "reason": "review awaiting agent; child review step (re)created",
            }
        if child["status"] == "COMPLETED":
            return _close_reviewed(conn, plan_id, review_step_id, child, registry_path)
        if child["status"] == "FAILED" and _is_step_recovered(conn, child["step_id"]):
            # FL-019: the review child failed, the agent recovered it through a
            # deviation sub-tree (judged + fixed) — close as reviewed.
            return _close_reviewed(conn, plan_id, review_step_id, child, registry_path, reopened=True)
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
            "project": _project_for_review(conn, plan_id, registry_path)[0],
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
    # S1 (spec §2.10): when we do NOT review, the reason is written down.
    review_skipped: str | None = None
    if new_plan_status == "COMPLETED":
        project, why, project_source = _project_for_review(conn, plan_id, registry_path)
        review_skipped = why if project is None else None
        if project is not None:
            child_id = _open_agent_review(conn, plan_id, review_step_id)
            return {
                "ready": False,
                "needs_agent_review": True,
                "project": project,
                "project_source": project_source,
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

    # Apply writes: review step status first, then plan status — plus the
    # skip reason (column + log line) in the SAME transaction, so a plan can
    # never end up COMPLETED without saying why it was not reviewed.
    with db.transaction(conn):
        db.update_step_status(conn, review_step_id, new_review_status, set_completed=True, commit=False)
        db.update_plan_status(conn, plan_id, new_plan_status, commit=False)
        if review_skipped:
            db.set_review_skip_reason(conn, plan_id, review_skipped, commit=False)
            telemetry.append_step_log(conn, review_step_id, f"[REVIEW SKIPPED] {review_skipped}", commit=False)

    out = {
        "ready": True,
        "plan_status": new_plan_status,
        "review_step_id": review_step_id,
        "review_status": new_review_status,
        "reason": reason,
    }
    if review_skipped:
        out["review_skipped"] = review_skipped
    return out
