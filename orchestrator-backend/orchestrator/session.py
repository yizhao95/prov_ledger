"""session — the degraded mode (DP phase 2, Task 7b): hooks alone keep the ledger.

Without the skills nobody publishes a plan, so nothing refreshes the state
graph and nothing asks for reasons. The Stop hook fills that gap on its own
terms: it records the session (`session_run`, the one table that may be
UPDATEd), and — when the repo is registered, the session published no plan,
the tree moved since the graph was built, the extensions do not say off, and
no refresh is already running — it queues a background graph refresh
(`init_project.sh --trigger session --session-id … --notify-orch-db …`).
When the refresh reports back (`refreshed`), the rules R0–R6 run over the
nodes the session changed under the placeholder plan id `session:<sid>`,
the rest is backstopped `unstated`, and a headline is computed and stored
for the session (never printed — nobody is there to read it).

What is lost without the skills is visible, never silent: no plan → no
headline response, no because, no asserted reason; `sessions_without_plan`
in selfcheck counts these sessions and their unstated share.
"""
from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import db, psg_bridge

SESSION_PLAN_PREFIX = "session:"
LOCK_NAME = ".refresh.lock"
_TS = "%Y-%m-%d %H:%M:%S"


def _now() -> str:
    return datetime.now(timezone.utc).strftime(_TS)


def session_plan_id(session_id: str) -> str:
    return SESSION_PLAN_PREFIX + session_id


def is_session_plan(plan_id: str | None) -> bool:
    return bool(plan_id) and str(plan_id).startswith(SESSION_PLAN_PREFIX)


def init_project_script() -> Path:
    return Path(__file__).resolve().parents[2] / "skills" / "project-state-graph" / "scripts" / "init_project.sh"


# ── session_run: the one mutable table ──────────────────────────────────────

def get(conn, session_id: str) -> dict | None:
    r = conn.execute("SELECT session_id, project, cwd, started_at, ended_at, psg_run_id, refresh_state, note FROM session_run WHERE session_id = ?",
                     (session_id,)).fetchone()
    return dict(zip(("session_id", "project", "cwd", "started_at", "ended_at", "psg_run_id", "refresh_state", "note"), r)) if r else None


def window(conn, session_id: str) -> tuple[str, str]:
    """(started, ended): the first thing the hooks saw of this session, and now."""
    first = conn.execute(
        "SELECT MIN(t) FROM (SELECT MIN(at) AS t FROM tool_call_log WHERE session_id = ? "
        "UNION ALL SELECT MIN(occurred_at) FROM utterance WHERE session_id = ?)", (session_id, session_id)).fetchone()[0]
    return (first or _now())[:19], _now()


def upsert(conn, session_id: str, **fields) -> None:
    conn.execute("INSERT OR IGNORE INTO session_run (session_id, started_at) VALUES (?, ?)", (session_id, fields.get("started_at") or _now()))
    if fields:
        cols = ", ".join(f"{k} = ?" for k in fields)
        conn.execute(f"UPDATE session_run SET {cols} WHERE session_id = ?", (*fields.values(), session_id))


def plans_in_session(conn, project: str, started: str, ended: str) -> list[str]:
    return [r[0] for r in conn.execute("SELECT plan_id FROM Plans WHERE project = ? AND created_at BETWEEN ? AND ? ORDER BY created_at",
                                       (project, started, ended))]


# ── the Stop decision ───────────────────────────────────────────────────────

def _git(repo: str, *args: str) -> str:
    try:
        p = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return ""
    return p.stdout.strip() if p.returncode == 0 else ""


def tree_moved(project: str, repo: str) -> tuple[bool, str]:
    """(moved, why): HEAD differs from the sha the graph was built at, or tracked files changed."""
    head = _git(repo, "rev-parse", "HEAD")
    registered = psg_bridge.registered_sha_for(project)
    if head and registered and head != registered:
        return True, f"HEAD {head[:7]} != registered {registered[:7]}"
    if _git(repo, "status", "--porcelain", "--untracked-files=no"):
        return True, "tracked files changed"
    return False, "tree unchanged since the graph was built"


def session_refresh_enabled(repo: str | None) -> bool:
    from . import extensions
    try:
        return extensions.current(repo).reasons_session_refresh != "off"
    except Exception:
        return True


def lock_path(project: str) -> Path | None:
    p = psg_bridge.db_path_for(project)
    return Path(p).parent / LOCK_NAME if p else None


def _spawn(argv: list[str], log_path: Path) -> int:
    """Detach the refresh: its own session, output to a log next to the graph."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "ab") as log:
        p = subprocess.Popen(argv, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                             start_new_session=True, cwd=str(init_project_script().parent))
    return p.pid


def on_stop(conn, data: dict, *, orch_db_path: str | None = None, commit: bool = True) -> dict:
    """The Stop hook body. Always records the session; queues a refresh only
    when every condition holds, and says which one failed otherwise."""
    session_id = str(data.get("session_id") or "")
    if not session_id:
        return {"refresh_state": "skipped", "note": "no session id"}
    cwd = data.get("cwd")
    project = psg_bridge.project_for_cwd(cwd)
    started, ended = window(conn, session_id)
    state, note, extra = "skipped", "", {}
    if not project:
        note = "cwd is not inside a registered project"
    else:
        repo = psg_bridge.repo_for(project) or cwd
        plans = plans_in_session(conn, project, started, ended)
        moved, why = tree_moved(project, repo)
        lock = lock_path(project)
        if plans:
            note = f"plans in session: {', '.join(plans[:5])} — the close already refreshed"
        elif not moved:
            note = why
        elif not session_refresh_enabled(repo):
            note = "reasons.session_refresh is off in the extensions file"
        elif lock is None:
            note = "no graph path registered"
        elif lock.exists():
            note = f"a refresh is already running ({lock})"
        else:
            lock.parent.mkdir(parents=True, exist_ok=True)
            lock.write_text(f"{session_id} {_now()}\n")
            argv = ["bash", str(init_project_script()), "--name", project, "--repo", repo, "--trigger", "session",
                    "--session-id", session_id, "--notify-orch-db", orch_db_path or str(db.DEFAULT_DB_PATH)]
            try:
                pid = _spawn(argv, lock.parent / "refresh.log")
                state, note = "queued", f"refresh queued (pid {pid}): {why}"
                extra["pid"] = pid
            except Exception as exc:
                try:
                    lock.unlink()
                except OSError:
                    pass
                state, note = "failed", f"could not start the refresh: {type(exc).__name__}: {exc}"[:200]
    upsert(conn, session_id, project=project, cwd=cwd, started_at=started, ended_at=ended, refresh_state=state, note=note)
    if commit:
        conn.commit()
    return {"session_id": session_id, "project": project, "refresh_state": state, "note": note, **extra}


# ── after the refresh: the rules, the backstop, a headline nobody prints ────

def evaluate_session(conn, *, project: str, session_id: str, psg_db_path: str | None, commit: bool = False) -> dict:
    from . import checks, context_pack, reasons, triggers
    pid = session_plan_id(session_id)
    trig = triggers.evaluate(conn, project=project, plan_id=pid, psg_db_path=psg_db_path, ask=True, commit=False)
    n_unstated = reasons.backstop_unstated(conn, project=project, plan_id=pid, psg_db_path=psg_db_path, commit=False)
    changed = psg_bridge.changed_node_keys(psg_db_path, pid)
    doc = None
    if changed:
        pack = context_pack.build(conn, project=project, targets=[c["node_key"] for c in changed], psg_db_path=psg_db_path,
                                  moment="close", session_id=session_id, record=False)      # nobody is shown anything here
        doc = checks.headline(conn, project=project, session_id=session_id, pack=pack, commit=False)
    if commit:
        conn.commit()
    return {"plan_id": pid, "changed": len(changed), "triggers": trig, "unstated": n_unstated,
            "headline_id": doc["headline_id"] if doc else None, "findings": len(doc["findings"]) if doc else 0}


def refreshed(conn, *, session_id: str, state: str, psg_run_id: int | None = None, psg_db_path: str | None = None,
              commit: bool = True) -> dict:
    """Called by init_project.sh --notify-orch-db when the session refresh ends."""
    row = get(conn, session_id)
    project = row["project"] if row else None
    lock = lock_path(project) if project else None
    if lock and lock.exists():
        try:
            lock.unlink()
        except OSError:
            pass
    if state not in ("done", "failed"):
        state = "failed"
    out = {"session_id": session_id, "refresh_state": state, "psg_run_id": psg_run_id}
    if state == "done" and project:
        psg = psg_db_path or psg_bridge.db_path_for(project)
        if psg_run_id is None:
            r = psg_bridge._query(psg, "SELECT MAX(id) FROM analysis_run WHERE plan_id = ?", (session_plan_id(session_id),))
            psg_run_id = int(r[0][0]) if r and r[0][0] is not None else None
            out["psg_run_id"] = psg_run_id
        try:
            out["evaluation"] = evaluate_session(conn, project=project, session_id=session_id, psg_db_path=psg, commit=False)
        except Exception as exc:  # the refresh happened; the evaluation failing is a note, not a crash
            out["evaluation_error"] = f"{type(exc).__name__}: {exc}"[:200]
    note = (row or {}).get("note") or ""
    if out.get("evaluation_error"):
        note = (note + " · evaluation failed: " + out["evaluation_error"])[:400]
    upsert(conn, session_id, refresh_state=state, psg_run_id=psg_run_id, note=note or None)
    if commit:
        conn.commit()
    return out


def main(argv=None) -> int:
    """python -m orchestrator.session refreshed --session-id S --state done|failed [--run-id N] [--orch-db P] [--psg-db P]"""
    import argparse
    import json
    ap = argparse.ArgumentParser(prog="orchestrator.session")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("refreshed")
    r.add_argument("--session-id", required=True); r.add_argument("--state", required=True, choices=["done", "failed"])
    r.add_argument("--run-id", type=int, default=None); r.add_argument("--orch-db", default=None); r.add_argument("--psg-db", default=None)
    args = ap.parse_args(argv)
    conn = db.open_db(args.orch_db or os.environ.get("ORCH_DB") or db.DEFAULT_DB_PATH)
    db.run_migrations(conn)
    try:
        print(json.dumps(refreshed(conn, session_id=args.session_id, state=args.state, psg_run_id=args.run_id, psg_db_path=args.psg_db), default=str))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
