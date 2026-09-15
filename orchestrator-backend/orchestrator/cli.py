"""provledger command line (DP phase 0/1): `python -m orchestrator.cli ...`
in the repo, `provledger ...` once installed.

  metrics plan <id>                       what one plan cost in tool calls
  metrics baseline [--since] [--write F]  median / p90 over completed plans
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from . import db, plan_metrics


def _db_path():
    return os.environ.get("ORCH_DB") or db.DEFAULT_DB_PATH


def _open():
    conn = db.open_db(_db_path())
    db.run_migrations(conn)
    return conn


def _metrics(args) -> int:
    conn = _open()
    try:
        if args.sub == "plan":
            print(json.dumps(plan_metrics.calls_for_plan(conn, args.plan_id), indent=1, sort_keys=True))
            return 0
        data = plan_metrics.baseline(conn, since=args.since)
        if args.write:
            plan_metrics.write_baseline(args.write, data)
        print(json.dumps(data, indent=1, sort_keys=True))
        return 0
    finally:
        conn.close()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="provledger",
                                description="provLedger decision provenance: what changed, why, and where the why came from.")
    sub = p.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("metrics", help="tool-call cost per plan and the performance baseline")
    ms = m.add_subparsers(dest="sub", required=True)
    mp = ms.add_parser("plan", help="metrics of one plan")
    mp.add_argument("plan_id")
    mb = ms.add_parser("baseline", help="median / p90 over completed plans")
    mb.add_argument("--since", default=None, help="only plans created at or after this timestamp")
    mb.add_argument("--write", default=None, metavar="FILE", help="also write the baseline JSON here")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "metrics":
        return _metrics(args)
    return 2


if __name__ == "__main__":
    sys.exit(main())
