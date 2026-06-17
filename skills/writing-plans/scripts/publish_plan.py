#!/usr/bin/env python3
"""Helper invoked by publish-plan.sh — parses + validates plan-input + calls init-plan.

Why a Python helper instead of pure bash:
  - JSON/YAML parsing in bash is brittle (jq for JSON only; no clean YAML path)
  - The orchestrator's api.initialize_plan() is the canonical write path; better
    to call it directly than reconstruct the CLI invocation as a string
  - Validation errors get clean Python exceptions → caller exits non-zero with
    a readable message

Usage (called by publish-plan.sh, not by the agent directly):
  ORCH_DB=/path/to/db python3 publish_plan.py /path/to/plan-input.{json,yaml}

Stdout: JSON {plan_id, step_ids, skills_recorded}
Stderr: human-readable error messages
Exit:   0 on success, non-zero on any validation/IO failure
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

# Make the orchestrator package importable (it lives on disk, not pip-installed)
ORCH_ROOT = Path.home() / "skill-workspace" / "orchestrator"
sys.path.insert(0, str(ORCH_ROOT))

from orchestrator import api, db  # noqa: E402

# impact_preflight lives beside this script (Phase D). stdlib-only, no orch deps.
import impact_preflight  # noqa: E402

# Mirror the CHECK constraint from migration 005_skill_activations.sql so we
# fail fast with a friendly message instead of a sqlite3.IntegrityError trace.
VALID_SKILL_SOURCES = {"iron-law", "auto-search", "explicit-mention", "deferred-load"}

REQUIRED_FIELDS = ("goal", "prefix", "steps")

# Registry of built project state-graphs (env-overridable for tests).
_REGISTRY_PATH = os.environ.get(
    "PSG_REGISTRY_PATH",
    str(Path.home() / "skill-workspace" / "project-graphs" / "projects.json"),
)


def _resolve_project_db(project: str) -> str:
    """Look up a tracked project's state-graph DB path from the registry.
    _die() (non-zero) if the registry or the named project is missing."""
    reg = Path(_REGISTRY_PATH)
    if not reg.exists():
        _die(f"project {project!r} named but registry not found at {reg}")
    try:
        data = json.loads(reg.read_text())
    except (ValueError, OSError) as e:
        _die(f"could not read project registry {reg}: {e}")
    for entry in data.get("projects", []):
        if entry.get("name") == project:
            db_path = entry.get("db_path")
            if not db_path or not Path(db_path).exists():
                _die(f"project {project!r} has no built graph at {db_path!r}")
            return db_path
    _die(f"project {project!r} not found in registry {reg}")
    return ""  # unreachable


def _ensure_impact_column(conn) -> None:
    """The live orchestrator.db already has a Plans table, so run_migrations is a
    no-op there — make sure the Phase D column exists before we write to it."""
    cols = [r[1] for r in conn.execute("PRAGMA table_info(Plans)").fetchall()]
    if "impact_context" not in cols:
        conn.execute("ALTER TABLE Plans ADD COLUMN impact_context TEXT")
        conn.commit()


def _die(msg: str, code: int = 1) -> None:
    """Print a one-line error to stderr and exit non-zero."""
    print(f"❌ publish-plan: {msg}", file=sys.stderr)
    sys.exit(code)


def _load_input(path: Path) -> dict:
    """Parse JSON or YAML based on file extension. Reject anything else."""
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
            import yaml  # local import: only needed for YAML path
        except ImportError:
            _die("YAML input requires PyYAML (pip install pyyaml)")
        try:
            return yaml.safe_load(text) or {}
        except yaml.YAMLError as e:
            _die(f"invalid YAML in {path}: {e}")
    _die(f"unsupported file extension {suffix!r}; use .json, .yaml, or .yml")
    return {}  # unreachable, satisfies type checker


def _validate(data: dict) -> None:
    """Validate the parsed dict matches plan-input.schema.json semantics.

    Raises via _die() with a message that mentions the offending field name
    so the user can fix their plan-input file without reading source code.
    """
    if not isinstance(data, dict):
        _die("plan-input must be a JSON/YAML object at the top level")
    for field in REQUIRED_FIELDS:
        if field not in data:
            _die(f"missing required field: '{field}' (required: {', '.join(REQUIRED_FIELDS)})")
    if not isinstance(data["goal"], str) or not data["goal"].strip():
        _die("'goal' must be a non-empty string")
    if not isinstance(data["prefix"], str) or not data["prefix"].strip():
        _die("'prefix' must be a non-empty string")
    steps = data["steps"]
    if not isinstance(steps, list) or len(steps) == 0:
        _die("'steps' must be a non-empty array (a plan with zero steps is meaningless)")
    for i, step in enumerate(steps):
        if isinstance(step, str):
            continue  # bare-string step is fine — description only
        if not isinstance(step, dict) or "description" not in step:
            _die(f"steps[{i}] must be a string OR an object with at least a 'description' field")
        if not isinstance(step["description"], str) or not step["description"].strip():
            _die(f"steps[{i}].description must be a non-empty string")
    skills = data.get("skills") or []
    if not isinstance(skills, list):
        _die("'skills' must be an array (or omitted entirely)")
    for i, sk in enumerate(skills):
        if not isinstance(sk, dict) or "name" not in sk or "source" not in sk:
            _die(f"skills[{i}] must be an object with 'name' and 'source' keys")
        if sk["source"] not in VALID_SKILL_SOURCES:
            _die(
                f"skills[{i}].source = {sk['source']!r} is invalid; "
                f"valid choices: {sorted(VALID_SKILL_SOURCES)}"
            )
    mr = data.get("max_revisions", 5)
    if not isinstance(mr, int) or mr < 1:
        _die("'max_revisions' must be a positive integer (default: 5)")


def _step_descriptions(steps: list) -> list[str]:
    """Normalize bare-string and {description: ...} step shapes to a list of strings."""
    out = []
    for s in steps:
        out.append(s if isinstance(s, str) else s["description"])
    return out


def main() -> None:
    if len(sys.argv) != 2:
        _die("usage: publish-plan.sh <plan-input.json|yaml>")
    data = _load_input(Path(sys.argv[1]))
    _validate(data)

    # Use ORCH_DB env var (tests set this to point at an ephemeral DB).
    # Fall back to the canonical location for normal use.
    db_path_env = os.environ.get("ORCH_DB")
    db_path = Path(db_path_env) if db_path_env else None
    conn = db.open_db(db_path) if db_path else db.open_db()
    # Only migrate if the schema is missing — run_migrations is NOT safely
    # re-runnable on an already-migrated DB (it raises 'duplicate column name').
    has_plans = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='Plans'"
    ).fetchone()
    if not has_plans:
        db.run_migrations(conn)

    # ── Phase D: plan-time pre-flight (un-skippable for tracked projects) ──
    # If the plan-input names a tracked project, declared_targets is MANDATORY
    # and the forward impact analysis runs synchronously here — "publish a plan"
    # and "do impact analysis" become one atomic act. Project-less plans skip
    # this entirely (backward compatible).
    project = data.get("project")
    impact_context = None
    if project:
        declared_targets = data.get("declared_targets")
        if not declared_targets or not isinstance(declared_targets, list):
            _die(
                f"plan names project {project!r} but 'declared_targets' is missing. "
                f"Forward impact analysis is mandatory for tracked projects: list "
                f"the symbols/columns this change will touch in 'declared_targets'."
            )
        graph_db = _resolve_project_db(project)
        impact_context = impact_preflight.compute_impact_context(
            graph_db, data.get("user_query") or "", declared_targets,
            project=project)
        _ensure_impact_column(conn)

    skills_activated_input = data.get("skills") or None
    # Translate the user-facing 'name' key to the api's 'skill_name' key.
    # We use 'name' in plan-input because it reads more naturally; the api
    # uses 'skill_name' for column-naming consistency with the DB.
    skills_activated = (
        [{"skill_name": s["name"], "source": s["source"]} for s in skills_activated_input]
        if skills_activated_input else None
    )
    result = api.initialize_plan(
        conn,
        original_goal=data["goal"],
        initial_steps=_step_descriptions(data["steps"]),
        plan_id_prefix=data["prefix"],
        max_revisions=data.get("max_revisions", 5),
        user_query=data.get("user_query"),
        skills_activated=skills_activated,
    )
    # Append the deterministic auto-review-and-complete marker step (migration
    # 006). This guarantees every newly-published plan has a terminal step that
    # the executing-plans complete-step / fail-step / finish-plan ops can flip
    # WITHOUT any LLM involvement. Legacy plans created before this change keep
    # working because the review_and_complete procedure falls back to plain
    # complete_plan when no review row is found.
    review_step_id = db.insert_review_step(conn, result["plan_id"])
    result["review_step_id"] = review_step_id
    # Persist the Phase D impact analysis on the Plan row (atomic with publish).
    if impact_context is not None:
        db.set_plan_impact_context(
            conn, result["plan_id"], json.dumps(impact_context, default=str))
        result["impact_context"] = {
            "targets": [t["name"] for t in impact_context["targets"]],
            "symbols": [{"name": s["name"], "status": s["status"]}
                        for s in impact_context["symbols"]],
            "upstream_assumptions": [u["table"]
                                     for u in impact_context["upstream_assumptions"]],
        }
    if skills_activated_input:
        result["skills_recorded"] = [s["name"] for s in skills_activated_input]
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
