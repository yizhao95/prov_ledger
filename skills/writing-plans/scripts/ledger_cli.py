#!/usr/bin/env python3
"""ledger_cli.py — manual entry CLI for the provLedger decision-memory (Phase E).

The decision-memory ledger is GRADUAL and OPT-IN: you add entries by hand as real
decisions get made and real failures get hit. It is never auto-populated. At plan
time, publish_plan.py fuzzy-matches a project plan against these entries and
surfaces relevant ones as REMINDERS (never blocks) in impact_context.

Usage:
    # record a decision + its rationale
    ledger_cli.py add --project sample-project --kind decision \\
        --statement "rolling-window split, not random split" \\
        --rationale "random split leaks temporal information" \\
        --subjects train_test_split,split \\
        --keywords split,rolling,temporal

    # record an anti-pattern (a failure worth remembering)
    ledger_cli.py add --project sample-project --kind anti_pattern \\
        --statement "changing UPC from BIGINT to STRING" \\
        --rationale "downstream joins broke on dtype mismatch" \\
        --subjects upc --keywords upc,bigint,string,dtype

    # list active entries for a project
    ledger_cli.py list --project sample-project

Environment:
    ORCH_DB   override the SQLite path (default: ~/skill-workspace/orchestrator.db).
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ledger_store  # noqa: E402

DEFAULT_DB = Path.home() / "skill-workspace" / "orchestrator.db"


def _db_path() -> str:
    return os.environ.get("ORCH_DB", str(DEFAULT_DB))


def _split_csv(value: str | None) -> list:
    if not value:
        return []
    return [v.strip() for v in value.split(",") if v.strip()]


# The orchestrator package (bundled orchestrator-backend/ first, the author's
# workspace install as fallback) — every write path migrates on open (FL-021).
_BUNDLED_ORCH = Path(__file__).resolve().parents[3] / "orchestrator-backend"
_DEV_ORCH = Path.home() / "skill-workspace" / "orchestrator"
_ORCH_ROOT = _BUNDLED_ORCH if (_BUNDLED_ORCH / "orchestrator" / "__init__.py").exists() else _DEV_ORCH


def _connect() -> sqlite3.Connection:
    """Open ORCH_DB migrated (FL-025: a raw sqlite3.connect on a DB nothing
    else had opened yet died with 'no such table: LedgerEntries')."""
    try:
        if str(_ORCH_ROOT) not in sys.path:
            sys.path.insert(0, str(_ORCH_ROOT))
        from orchestrator import db as orch_db  # noqa: WPS433
        conn = orch_db.open_db(_db_path())
        orch_db.run_migrations(conn)
        return conn
    except ImportError:            # no orchestrator package around: raw open, as before
        conn = sqlite3.connect(_db_path())
        conn.row_factory = sqlite3.Row
        return conn


def cmd_add(args) -> int:
    conn = _connect()
    try:
        try:
            eid = ledger_store.add_entry(
                conn, project=args.project, kind=args.kind,
                statement=args.statement, rationale=args.rationale or "",
                subjects=_split_csv(args.subjects),
                keywords=_split_csv(args.keywords),
                source=args.source or "manual",
                plan_id=args.plan_id, why_ref=args.why_ref,
                why_visibility=args.why_visibility)
        except ValueError as e:
            print(f"❌ {e}", file=sys.stderr)
            return 1
    finally:
        conn.close()
    print(f"✅ added ledger entry id={eid} ({args.kind}) for project {args.project!r}")
    return 0


def cmd_list(args) -> int:
    conn = _connect()
    try:
        rows = ledger_store.get_entries(
            conn, args.project, include_superseded=args.all)
    finally:
        conn.close()
    if not rows:
        print(f"(no entries for project {args.project!r})")
        return 0
    for r in rows:
        print(f"[{r['id']}] ({r['kind']}/{r['status']}) {r['statement']}")
        if r.get("rationale"):
            print(f"      rationale: {r['rationale']}")
        if r.get("subjects"):
            print(f"      subjects: {', '.join(r['subjects'])}")
        if r.get("keywords"):
            print(f"      keywords: {', '.join(r['keywords'])}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Manual decision-memory ledger (provLedger Phase E). "
                    "Add entries by hand as real decisions/failures occur.")
    sub = p.add_subparsers(dest="command", required=True)

    a = sub.add_parser("add", help="record a decision or anti_pattern")
    a.add_argument("--project", required=True)
    a.add_argument("--kind", required=True, choices=list(ledger_store.VALID_KINDS))
    a.add_argument("--statement", required=True)
    a.add_argument("--rationale", default="")
    a.add_argument("--subjects", default="",
                   help="comma-separated symbols/tables; may include node_keys (nk_...) or owner.column "
                        "— constraints are matched on these exactly")
    a.add_argument("--keywords", default="", help="comma-separated match terms")
    a.add_argument("--source", default="manual")
    a.add_argument("--plan-id", default=None, help="provenance plan that produced this entry")
    a.add_argument("--why-ref", default=None,
                   help="reference to the source of the WHY (decision doc, meeting notes)")
    a.add_argument("--why-visibility", default="shared", choices=list(ledger_store.VALID_VISIBILITY),
                   help="restricted: the rationale never leaves the ledger, only --why-ref does")
    a.set_defaults(func=cmd_add)

    l = sub.add_parser("list", help="list entries for a project")
    l.add_argument("--project", required=True)
    l.add_argument("--all", action="store_true", help="include superseded")
    l.set_defaults(func=cmd_list)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
