"""CLI: build a [project]-state-graph.db by running all analyzers over a repo.

Usage:
    python -m analyzer <repo_path> --project <name> [--db-path PATH | --out-dir DIR]
                       [--plan-id ID] [--step-id ID] [--trigger manual|review|update]
    python -m analyzer history <db_path> <qualified_name|node_key>
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from typing import Optional

from . import (
    api_refs,
    cards,
    data_model,
    dataflow,
    dataflow_types,
    de_overlay,
    history,
    namesets,
    leakage,
    ml_overlay,
    pipeline,
    profiles,
    py_ast,
    routes,
    sql_refs,
    store,
    ts_polyglot,
    unresolved,
    walker,
)


def _resolve_db_path(project: str, db_path: Optional[str], out_dir: Optional[str]) -> str:
    if db_path:
        return db_path
    directory = out_dir or os.getcwd()
    return os.path.join(directory, f"{project}-state-graph.db")


def git_info(repo_path: str) -> dict:
    """Return {'commit_sha': str|None, 'dirty': bool} for a repo.

    Gracefully returns commit_sha=None when repo_path is not a git repo.
    """
    def _run(*args):
        return subprocess.run(
            ["git", "-C", repo_path, *args],
            capture_output=True, text=True,
        )

    head = _run("rev-parse", "HEAD")
    if head.returncode != 0:
        return {"commit_sha": None, "dirty": False}
    sha = head.stdout.strip() or None
    status = _run("status", "--porcelain")
    dirty = bool(status.stdout.strip())
    return {"commit_sha": sha, "dirty": dirty}


def run(repo_path: str, project: str, db_path: str, build_cards: bool = True,
        plan_id: Optional[str] = None, step_id: Optional[str] = None,
        trigger: str = "manual") -> str:
    info = git_info(repo_path)
    if info["dirty"]:
        print(
            f"WARNING: working tree at {repo_path} has uncommitted changes; "
            "the state-graph may not match committed code.",
            file=sys.stderr,
        )
    # Phase 5: the repo's provledger-extensions.json may patch the analyzers'
    # name sets — configured here, read by the analyzers via namesets.get().
    namesets.configure(repo_path)
    ext_fp = namesets.extensions_fingerprint(repo_path)
    conn = store.init_db(db_path)
    run_id = store.start_run(conn, project_name=project, commit_sha=info["commit_sha"],
                             plan_id=plan_id, step_id=step_id, trigger=trigger,
                             extensions_json=json.dumps(ext_fp, sort_keys=True) if ext_fp else None)
    store.reset_graph(conn)  # PSG-C1: idempotent rebuild — clear prior graph rows
    try:
        file_map = walker.walk(conn, repo_path)
        py_ast.analyze(conn, repo_path, file_map)
        unresolved.analyze(conn, repo_path, file_map)
        dataflow.analyze(conn, repo_path, file_map)
        dataflow_types.analyze(conn, repo_path, file_map)
        data_model.analyze(conn, repo_path, file_map)
        pipeline.analyze(conn, repo_path, file_map)
        sql_refs.analyze(conn, repo_path, file_map)
        api_refs.analyze(conn, repo_path, file_map)
        routes.analyze(conn, repo_path, file_map)
        ts_polyglot.analyze(conn, repo_path, file_map)
        ml_overlay.analyze(conn, repo_path, file_map)
        de_overlay.analyze(conn, repo_path, file_map)
        leakage.analyze(conn, repo_path, file_map)  # Phase 4.1 silent-failure gate
        profiles.analyze(conn, repo_path, file_map)
        if build_cards:
            cards.build_symbol_cards(conn)  # also builds consistency cards
        # History layer (spec §2.5): snapshot this rebuild's rows (selected by
        # run_id IS NULL, hence BEFORE stamp_run), match against the previous
        # run, assign node_keys and append events.
        history.snapshot_run(conn, repo_path, run_id)
        history.resolve(conn, run_id)
        if build_cards:
            cards.attach_history(conn)  # symbol_card gains node_key + recent history
        store.stamp_run(conn, run_id)  # PSG-D2: tag this rebuild's rows
    finally:
        store.finish_run(conn, run_id)
        conn.close()
    return db_path


def history_main(argv) -> int:
    """`analyzer history <db> <qualified_name|node_key>` — print the event
    stream of one node with run attribution, then the approximate token cost
    (len(text)//4, v2 criterion "query token cost")."""
    parser = argparse.ArgumentParser(prog="analyzer history",
                                     description="Print the history of one node.")
    parser.add_argument("db_path")
    parser.add_argument("node", help="qualified_name (latest) or node_key (nk_...)")
    args = parser.parse_args(argv)
    conn = store.init_db(args.db_path)
    try:
        events = history.events_of(conn, args.node)
    finally:
        conn.close()
    if not events:
        print(f"no history for {args.node}")
        return 1
    lines = [f"history of {args.node} ({len(events)} events)"]
    for e in events:
        sha = (e["commit_sha"] or "")[:7]
        try:
            ext_sha = (json.loads(e.get("extensions_json") or "null") or {}).get("sha256")
        except ValueError:
            ext_sha = None
        lines.append(f"  run={e['run_id']} seq={e['seq']} {e['event_type']} [{e['tier']}] "
                     f"plan={e['plan_id'] or '-'} step={e['step_id'] or '-'} trigger={e['trigger'] or '-'} "
                     f"sha={sha or '-'} ext={ext_sha[:8] if ext_sha else 'none'} at={e['created_at']} "
                     f"payload={json.dumps(e['payload'], sort_keys=True)}")
    text = "\n".join(lines)
    print(text)
    print(f"approx_tokens={len(text) // 4}")
    return 0


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "history":
        return history_main(argv[1:])
    parser = argparse.ArgumentParser(
        prog="analyzer",
        description="Build a project state-graph SQLite DB from a repo.",
    )
    parser.add_argument("repo_path", help="path to the repository to analyze")
    parser.add_argument("--project", required=True, help="project name")
    parser.add_argument("--db-path", default=None, help="explicit DB file path")
    parser.add_argument("--out-dir", default=None,
                        help="directory for the default [project]-state-graph.db")
    parser.add_argument("--no-cards", action="store_true",
                        help="skip building consistency/symbol cards")
    parser.add_argument("--plan-id", default=None, help="orchestrator plan that caused this run")
    parser.add_argument("--step-id", default=None, help="orchestrator step that caused this run")
    parser.add_argument("--trigger", default="manual", help="manual | review | update | ...")
    args = parser.parse_args(argv)

    db_path = _resolve_db_path(args.project, args.db_path, args.out_dir)
    out = run(args.repo_path, args.project, db_path, build_cards=not args.no_cards,
              plan_id=args.plan_id, step_id=args.step_id, trigger=args.trigger)
    print(f"state-graph written to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
