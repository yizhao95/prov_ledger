"""provledger command line (DP phase 0/1): `python -m orchestrator.cli ...`
in the repo, `provledger ...` once installed.

  metrics plan <id>                       what one plan cost in tool calls
  metrics baseline [--since] [--write F]  median / p90 over completed plans
  note "<words>" --at <when> [...]        record something that was said, after the fact
  reasons reclass-status                  the state of the legacy-reason migration
  headline show|respond|ack <plan> …      the plan headline and its answers
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


# ── note: the after-the-fact entry point (DP phase 1, Task 6) ─────────────────

def _parse_ref(spec: str) -> dict:
    """kind=email label="re: weeks" uri=mail:1  →  {kind, label, uri}. Quotes are the shell's job."""
    out: dict = {}
    for part in spec.split(","):
        k, sep, v = part.strip().partition("=")
        if not sep:
            raise SystemExit(f"--ref needs key=value pairs (kind=…,label=…[,uri=…]), got {part!r}")
        out[k.strip()] = v.strip()
    if "kind" not in out or "label" not in out:
        raise SystemExit("--ref needs at least kind=… and label=…")
    return out


def _check_at(value: str) -> str:
    """An ISO-ish timestamp for occurred_at (YYYY-MM-DD[ HH:MM[:SS]]); the record time is the DB's."""
    from datetime import datetime
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
    raise SystemExit(f"--at must be a date/time like 2026-09-15 14:30, got {value!r}")


def _note(args) -> int:
    from . import provenance, psg_bridge
    text = args.text.strip()
    if not text:
        raise SystemExit("note needs the words that were said")
    at = _check_at(args.at)
    conn = _open()
    try:
        project = args.project or psg_bridge.project_for_cwd(os.getcwd())
        with db.transaction(conn):
            uid = provenance.insert_utterance(conn, session_id=args.session or "note", project=project, plan_id=args.plan,
                                              text=text, occurred_at=at, commit=False)
            refs = []
            for spec in args.ref or ():
                r = _parse_ref(spec)
                refs.append(provenance.insert_reference(conn, project=project or "-", kind=r["kind"], label=r["label"],
                                                        occurred_at=at, uri=r.get("uri") or None, commit=False))
            reason_id = None
            if args.node:
                if not project:
                    raise SystemExit("--node needs a project (--project, or run inside a registered repo)")
                key = args.node if args.node.startswith("nk_") else (psg_bridge.node_key_of(psg_bridge.db_path_for(project), args.node) or args.node)
                reason_id = provenance.insert_reason(conn, project=project, plan_id=args.plan or "note", node_key=key,
                                                     kind=args.kind, verbatim=(uid, 0, len(text)), refs=refs,
                                                     recorded_by="human", occurred_at=at, commit=False)
        out = {"utterance_id": uid, "project": project, "plan_id": args.plan, "occurred_at": at, "reference_ids": refs,
               "reason_id": reason_id}
        if reason_id is not None:
            out["evidence_level"] = provenance.get_reason(conn, reason_id)["evidence_level"]
        print(json.dumps(out, indent=1, sort_keys=True))
        return 0
    finally:
        conn.close()


def _ask_basis(conn, since: str | None) -> dict:
    from . import psg_bridge
    sql = "SELECT project, plan_id, node_key, basis FROM trigger_log WHERE verdict = 'ask'"
    params: list = []
    if since:
        sql += " AND at >= ?"
        params.append(since)
    rows = conn.execute(sql + " ORDER BY id", params).fetchall()
    groups: dict = {}
    dbs: dict = {}
    for project, plan_id, node_key, basis in rows:
        db_path = dbs.setdefault(project, psg_bridge.db_path_for(project))
        nt = psg_bridge._query(db_path, "SELECT node_type FROM node_snapshot WHERE node_key = ? ORDER BY run_id DESC, id DESC LIMIT 1", (node_key,))
        node_type = nt[0][0] if nt else "?"
        g = groups.setdefault((plan_id, project, node_type), {"plan_id": plan_id, "project": project, "node_type": node_type, "asks": 0, "basis": []})
        g["asks"] += 1
        if basis and basis not in g["basis"]:
            g["basis"].append(basis)
    return {"since": since, "asks": len(rows), "groups": [groups[k] for k in sorted(groups)]}


def _headline_cmd(args) -> int:
    from . import checks
    conn = _open()
    try:
        if args.sub == "show":
            doc = checks.latest(conn, plan_id=args.plan_id)
            if doc is None:
                print(f"no headline for plan {args.plan_id}")
                return 1
            print(checks.render(doc))
            return 0
        by = "human" if args.sub == "ack" else args.by
        action = "proceed" if args.sub == "ack" else args.action
        rid = checks.respond(conn, plan_id=args.plan_id, finding_id=args.finding_id, action=action,
                             rationale=args.rationale, by=by, cites=args.cite)
        print(json.dumps({"response_id": rid, "plan_id": args.plan_id, "finding_id": args.finding_id, "action": action, "by": by,
                          "adopted": len(set(args.cite))}, indent=1))
        return 0
    finally:
        conn.close()


def _reasons_cmd(args) -> int:
    conn = _open()
    try:
        if args.sub == "ask-basis":
            print(json.dumps(_ask_basis(conn, args.since), indent=1, sort_keys=True))
            return 0
        if args.sub == "reclass-status":
            row = conn.execute("SELECT value, at FROM migration_state WHERE key='dp_reclass'").fetchone()
            counts = dict(conn.execute("SELECT tier, COUNT(*) FROM change_reason GROUP BY tier").fetchall())
            print(json.dumps({"dp_reclass": (row[0] if row else None), "at": (row[1] if row else None),
                              "change_reason_by_tier": counts,
                              "node_reason_rows": conn.execute("SELECT COUNT(*) FROM node_reason").fetchone()[0]}, indent=1, sort_keys=True))
            return 0
        return 2
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
    n = sub.add_parser("note", help="record something that was said (a verbal decision), with the time it happened")
    n.add_argument("text", help="the words, verbatim")
    n.add_argument("--at", required=True, help="when it was said (YYYY-MM-DD HH:MM[:SS]); the record time is the database's")
    n.add_argument("--project", default=None, help="registered project (default: the one whose repo contains the cwd)")
    n.add_argument("--plan", default=None, help="plan id to attribute the words to")
    n.add_argument("--node", default=None, help="qualified name or node_key: also record a stated reason for it spanning the whole note")
    n.add_argument("--kind", default="organizational", choices=["technical", "organizational", "mixed"])
    n.add_argument("--ref", action="append", default=[], metavar="kind=…,label=…[,uri=…]",
                   help="a source to register and link (email, meeting, chat, ticket, doc, commit, verbal, other); repeatable")
    n.add_argument("--session", default=None, help=argparse.SUPPRESS)
    h = sub.add_parser("headline", help="the plan headline: what the two-layer check found, and how it was answered")
    hs = h.add_subparsers(dest="sub", required=True)
    hshow = hs.add_parser("show", help="print a plan's latest headline"); hshow.add_argument("plan_id")
    hr = hs.add_parser("respond", help="answer one finding (revise | proceed) with a rationale; cited records become adopted")
    hr.add_argument("plan_id"); hr.add_argument("finding_id")
    hr.add_argument("--action", required=True, choices=["revise", "proceed"]); hr.add_argument("--rationale", default=None)
    hr.add_argument("--by", default="agent", choices=["agent", "human"]); hr.add_argument("--cite", action="append", type=int, default=[], metavar="REASON_ID")
    ha = hs.add_parser("ack", help="a person proceeds past a finding (by human)"); ha.add_argument("plan_id"); ha.add_argument("finding_id")
    ha.add_argument("--rationale", default=None); ha.add_argument("--cite", action="append", type=int, default=[], metavar="REASON_ID")
    r = sub.add_parser("reasons", help="the reasons ledger")
    rs = r.add_subparsers(dest="sub", required=True)
    rs.add_parser("reclass-status", help="whether the legacy node_reason / ledger rows were migrated into change_reason, and the tier counts")
    ab = rs.add_parser("ask-basis", help="the close-time questions (trigger_log verdict ask) grouped by plan and node type — what the rules did not recognise")
    ab.add_argument("--since", default=None, help="only verdicts at or after this timestamp")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "metrics":
        return _metrics(args)
    if args.cmd == "note":
        return _note(args)
    if args.cmd == "reasons":
        return _reasons_cmd(args)
    if args.cmd == "headline":
        return _headline_cmd(args)
    return 2


if __name__ == "__main__":
    sys.exit(main())
