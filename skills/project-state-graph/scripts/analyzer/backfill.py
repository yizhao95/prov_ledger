"""Backfill (phase 7 Task 3): replay a repo's past commits into a FRESH
state-graph DB so the history layer starts before provLedger was installed.
Each sampled commit is checked out into a detached git worktree and analyzed
with the ordinary pipeline (`cli.run`, trigger='backfill', plan_id NULL, the
commit sha stamped explicitly); the run's predecessor is simply the previous
backfill run, so node_keys, renames, moves and removals resolve exactly as
they would have live. A DB that already holds live (non-backfill) keyed snapshots is refused
unless the caller asks for a fresh one — backfill never rewrites a history
that was observed for real. Resumable: commits that already have a finished,
non-aborted run are skipped.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from . import cli, store


class BackfillRefused(RuntimeError):
    """The target DB already holds observed history."""


def _git(repo: str, *args: str) -> str:
    return subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, check=True).stdout.strip()


def commits(repo: str, since: str, until: str = "HEAD", subdir: str | None = None) -> list[str]:
    """Oldest-first shas from `since` (inclusive) to `until` (inclusive); with
    `subdir`, only commits that touch that path (`git rev-list -- subdir`)."""
    since_sha = _git(repo, "rev-parse", "--verify", f"{since}^{{commit}}")
    path = ["--", subdir] if subdir else []
    try:
        out = _git(repo, "rev-list", "--reverse", f"{since_sha}^..{until}", *path)
    except subprocess.CalledProcessError:                      # `since` is a root commit: no parent
        out = _git(repo, "rev-list", "--reverse", until, *path)
        shas = out.split()
        if not subdir:
            return shas[shas.index(since_sha):] if since_sha in shas else shas
        all_shas = _git(repo, "rev-list", "--reverse", until).split()
        start = all_shas.index(since_sha) if since_sha in all_shas else 0
        allowed = set(all_shas[start:])
        return [s for s in shas if s in allowed]
    return out.split()


def keyed_snapshots(db_path: str) -> int:
    """Keyed snapshots that were OBSERVED live (runs whose trigger is not
    'backfill'). Earlier backfill runs are not counted — that is what a
    resume continues from."""
    if not os.path.exists(db_path):
        return 0
    conn = store.init_db(db_path)
    try:
        return int(conn.execute(
            """SELECT COUNT(*) FROM node_snapshot s JOIN analysis_run a ON a.id = s.run_id
               WHERE s.node_key <> '' AND COALESCE(a.trigger, '') <> 'backfill'""").fetchone()[0])
    finally:
        conn.close()


def done_commits(db_path: str, project: str) -> set[str]:
    if not os.path.exists(db_path):
        return set()
    conn = store.init_db(db_path)
    try:
        return {r[0] for r in conn.execute(
            "SELECT commit_sha FROM analysis_run WHERE project_name=? AND commit_sha IS NOT NULL "
            "AND finished_at IS NOT NULL AND COALESCE(aborted, 0) = 0 AND trigger='backfill'", (project,))}
    finally:
        conn.close()


def _run_events(db_path: str, run_id: int) -> dict:
    conn = store.init_db(db_path)
    try:
        return {r[0]: r[1] for r in conn.execute(
            "SELECT event_type, COUNT(*) FROM node_event WHERE run_id=? GROUP BY event_type", (run_id,))}
    finally:
        conn.close()


def run(repo: str, project: str, db_path: str, since: str, *, until: str = "HEAD", every: int = 1,
        max_commits: int | None = None, fresh_db: bool = False, subdir: str | None = None, log=None) -> dict:
    """Replay commits since..until (sampled every `every`) into db_path.
    `subdir` (relative to the repo) replays only the commits touching it and
    analyzes that directory alone — a project that lives inside a bigger repo.
    Returns {commits, sampled, runs, skipped, events: {type: n}, ambiguous, shas}."""
    repo = str(Path(repo).resolve())
    log = log or (lambda s: None)
    if every < 1:
        raise ValueError("every must be >= 1")
    if fresh_db:
        for suffix in ("", "-wal", "-shm", "-journal"):
            try:
                os.remove(db_path + suffix)
            except FileNotFoundError:
                pass
    keyed = keyed_snapshots(db_path)
    if keyed:
        raise BackfillRefused(f"{db_path} already holds {keyed} keyed snapshot(s) — backfill only writes a fresh "
                              "history; pass --fresh-db to start over (the observed history would be lost)")
    all_shas = commits(repo, since, until, subdir)
    sampled = all_shas[::every]
    if sampled and all_shas[-1] not in sampled:
        sampled.append(all_shas[-1])                            # the range's tip is always analyzed
    if max_commits is not None:
        sampled = sampled[:max_commits]
    done = done_commits(db_path, project)
    stats = {"commits": len(all_shas), "sampled": len(sampled), "runs": 0, "skipped": 0,
             "events": {}, "ambiguous": 0, "shas": []}
    for i, sha in enumerate(sampled):
        if sha in done:
            stats["skipped"] += 1
            log(f"[{i + 1}/{len(sampled)}] {sha[:7]} already backfilled — skipped")
            continue
        wt = tempfile.mkdtemp(prefix="provledger-backfill-")
        os.rmdir(wt)                                            # git wants to create it
        _git(repo, "worktree", "add", "--detach", wt, sha)
        try:
            last = i == len(sampled) - 1
            target = os.path.join(wt, subdir) if subdir else wt
            if not os.path.isdir(target):
                raise FileNotFoundError(f"{subdir!r} does not exist at {sha[:7]}")
            cli.run(target, project, db_path, build_cards=last, trigger="backfill", commit_sha=sha)
            conn = store.init_db(db_path)
            try:
                run_id = store.latest_run_id(conn)
            finally:
                conn.close()
            ev = _run_events(db_path, run_id)
            for k, v in ev.items():
                stats["events"][k] = stats["events"].get(k, 0) + v
            stats["ambiguous"] += ev.get("identity_ambiguous", 0)
            stats["runs"] += 1
            stats["shas"].append(sha)
            log(f"[{i + 1}/{len(sampled)}] {sha[:7]} run={run_id} " + " ".join(f"{k}={v}" for k, v in sorted(ev.items())))
        finally:
            subprocess.run(["git", "-C", repo, "worktree", "remove", "--force", wt], capture_output=True)
            shutil.rmtree(wt, ignore_errors=True)
            subprocess.run(["git", "-C", repo, "worktree", "prune"], capture_output=True)
    return stats
