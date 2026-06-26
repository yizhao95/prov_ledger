"""CLI: build a [project]-state-graph.db by running all analyzers over a repo.

Usage:
    python -m analyzer <repo_path> --project <name> [--db-path PATH | --out-dir DIR]
"""
from __future__ import annotations

import argparse
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


def run(repo_path: str, project: str, db_path: str, build_cards: bool = True) -> str:
    info = git_info(repo_path)
    if info["dirty"]:
        print(
            f"WARNING: working tree at {repo_path} has uncommitted changes; "
            "the state-graph may not match committed code.",
            file=sys.stderr,
        )
    conn = store.init_db(db_path)
    run_id = store.start_run(conn, project_name=project,
                             commit_sha=info["commit_sha"])
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
        profiles.analyze(conn, repo_path, file_map)
        if build_cards:
            cards.build_symbol_cards(conn)  # also builds consistency cards
        store.stamp_run(conn, run_id)  # PSG-D2: tag this rebuild's rows
    finally:
        store.finish_run(conn, run_id)
        conn.close()
    return db_path


def main(argv=None) -> int:
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
    args = parser.parse_args(argv)

    db_path = _resolve_db_path(args.project, args.db_path, args.out_dir)
    out = run(args.repo_path, args.project, db_path, build_cards=not args.no_cards)
    print(f"state-graph written to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
