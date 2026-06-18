#!/usr/bin/env python3
"""Backfill prior markdown plan files into the SQLite orchestrator DB.

Parses the `> **plan_id:** \`...\`` header + Status Dashboard table + Recent log
section from each .md file in ~/skill-workspace/plans/ and inserts corresponding
Plans + Steps rows.

Idempotent: skips plans whose plan_id already exists. Use --force to overwrite.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "orchestrator"))

from orchestrator import db  # noqa: E402

PLAN_DIR = Path.home() / "skill-workspace" / "plans"

# Map our markdown emoji status → DB enum values
STATUS_MAP = {
    "✅ completed": "COMPLETED",
    "✅": "COMPLETED",
    "completed": "COMPLETED",
    "🚧 in-progress": "IN_PROGRESS",
    "🚧": "IN_PROGRESS",
    "in-progress": "IN_PROGRESS",
    "⏳ pending": "PENDING",
    "⏳": "PENDING",
    "pending": "PENDING",
    "❌ failed": "FAILED",
    "❌": "FAILED",
    "❌ blocked": "FAILED",
    "🚫 blocked": "FAILED",
}

PLAN_STATUS_MAP = {
    "✅ completed": "COMPLETED",
    "🚧 in-progress": "IN_PROGRESS",
    "❌ failed": "FAILED",
}


def normalize_status(raw: str) -> str:
    raw = raw.strip().lower()
    for key, val in STATUS_MAP.items():
        if key.lower() in raw:
            return val
    return "PENDING"


def parse_plan_file(path: Path) -> dict | None:
    """Returns {plan_id, goal, status, revision_count, steps: [...], recent_log}."""
    text = path.read_text()

    plan_id_m = re.search(r"\*\*plan_id:\*\*\s*`([^`]+)`", text)
    goal_m = re.search(r"\*\*Goal:\*\*\s*(.+?)(?:\n\n|\n\*\*)", text, re.DOTALL)
    status_m = re.search(r"\*\*Status:\*\*\s*(.+)", text)
    rev_m = re.search(r"\*\*revision_count:\*\*\s*(\d+)\s*/\s*max\s*(\d+)", text)

    if not plan_id_m:
        return None

    plan_id = plan_id_m.group(1)
    goal = goal_m.group(1).strip() if goal_m else "(unknown)"
    status_raw = status_m.group(1).strip() if status_m else ""
    plan_status = "COMPLETED" if "completed" in status_raw.lower() else (
        "IN_PROGRESS" if "in-progress" in status_raw.lower() else "FAILED"
    )
    revision_count = int(rev_m.group(1)) if rev_m else 0
    max_revisions = int(rev_m.group(2)) if rev_m else 5

    # Parse Status Dashboard table — format: | # | Task | Status | Started | Completed | Depth | Parent |
    steps = []
    in_dashboard = False
    for line in text.splitlines():
        if "## Status Dashboard" in line:
            in_dashboard = True
            continue
        if in_dashboard:
            if line.startswith("## ") or line.startswith("**Verification"):
                in_dashboard = False
                continue
            if not line.strip().startswith("|"):
                continue
            cells = [c.strip() for c in line.split("|")[1:-1]]
            if len(cells) < 7:
                continue
            if cells[0] in ("#", "---") or "---" in cells[0]:
                continue
            label, desc, status_raw, started, completed, depth, parent = cells[:7]
            steps.append({
                "label": label,
                "description": desc,
                "status": normalize_status(status_raw),
                "started_at": None if started in ("—", "-", "") else started,
                "completed_at": None if completed in ("—", "-", "") else completed,
                "depth_level": int(depth) if depth.isdigit() else 0,
                "parent": None if parent in ("—", "-", "") else parent,
            })

    # Parse Recent log block (between ``` fences after "## Recent log")
    recent_log = ""
    log_m = re.search(r"## Recent log\s*\n+```\n(.*?)\n```", text, re.DOTALL)
    if log_m:
        recent_log = log_m.group(1).strip()

    return {
        "plan_id": plan_id,
        "goal": goal,
        "status": plan_status,
        "revision_count": revision_count,
        "max_revisions": max_revisions,
        "steps": steps,
        "recent_log": recent_log,
        "source_file": str(path),
    }


def backfill(parsed: dict, conn, force: bool = False, dry_run: bool = False) -> str:
    plan_id = parsed["plan_id"]
    existing = db.get_plan(conn, plan_id)
    if existing and not force:
        return f"  ⏭️  SKIP {plan_id} (already exists; --force to overwrite)"
    if existing and force:
        if dry_run:
            return f"  🔄 WOULD overwrite {plan_id}"
        conn.execute("DELETE FROM Steps WHERE plan_id = ?", (plan_id,))
        conn.execute("DELETE FROM Plans WHERE plan_id = ?", (plan_id,))
        conn.commit()

    if dry_run:
        return f"  📥 WOULD insert {plan_id}: {len(parsed['steps'])} steps"

    db.insert_plan(
        conn, plan_id, parsed["goal"],
        max_revisions=parsed["max_revisions"], status=parsed["status"],
    )
    # Update revision_count separately (insert_plan defaults to 0)
    for _ in range(parsed["revision_count"]):
        db.increment_revision(conn, plan_id)
    if parsed["status"] in ("COMPLETED", "FAILED"):
        db.update_plan_status(conn, plan_id, parsed["status"])

    for i, s in enumerate(parsed["steps"]):
        sid = f"{plan_id}-{s['label']}"
        # Distribute the recent_log: put on the last step
        log = parsed["recent_log"] if i == len(parsed["steps"]) - 1 else ""
        db.insert_step(
            conn, sid, plan_id, s["description"],
            execution_order=i, depth_level=s["depth_level"],
            status=s["status"],
            started_at=s["started_at"], completed_at=s["completed_at"],
            log_context=log,
        )
    return f"  ✅ INSERTED {plan_id}: {len(parsed['steps'])} steps + {len(parsed['recent_log'])}-char log on last step"


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", help="SQLite path (default: ~/skill-workspace/orchestrator.db)")
    parser.add_argument("--force", action="store_true", help="Overwrite existing plans")
    parser.add_argument("--dry-run", action="store_true", help="Don't actually insert")
    parser.add_argument("--only", help="Only backfill plans matching this glob pattern")
    args = parser.parse_args()

    conn = db.open_db(Path(args.db) if args.db else db.DEFAULT_DB_PATH)
    db.run_migrations(conn)

    pattern = args.only if args.only else "*.md"
    plan_files = sorted(PLAN_DIR.glob(pattern))
    if not plan_files:
        print(f"No plans found in {PLAN_DIR} matching {pattern}")
        return

    print(f"Scanning {len(plan_files)} plan file(s)...")
    for path in plan_files:
        parsed = parse_plan_file(path)
        if not parsed:
            print(f"  ⚠️  SKIP {path.name} (no plan_id header)")
            continue
        print(f"\n{path.name}:")
        print(f"  plan_id: {parsed['plan_id']}")
        print(f"  steps:   {len(parsed['steps'])}")
        print(f"  status:  {parsed['status']}  rev: {parsed['revision_count']}/{parsed['max_revisions']}")
        print(backfill(parsed, conn, force=args.force, dry_run=args.dry_run))


if __name__ == "__main__":
    main()
