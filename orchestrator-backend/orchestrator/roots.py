"""roots — a plan's root cause (task-level redesign, step 2).

A plan is a task. Why it exists usually started earlier, in the user's own
words, and several plans may carry that root forward: the window was set to two
months, then a holiday in the window made the plain average misleading, so a
second task added a year-on-year comparison. Both tasks answer "why" with the
same root.

At publish the agent says which (`plan_root`, migration 035, append-only):

    {"kind": "new", "utterance_id": 42, "basis": "…"}     this plan starts a root
    {"kind": "continues", "plan_id": "<P>", "basis": "…"}  it carries P's root on
    nothing                                                 kind unknown, by the system

Whether two tasks share a root is the agent's judgement, recorded as asserted;
a person may confirm or reject it later with a new row, and the latest row of a
plan counts. Continuing a plan resolves to where its root started, so every
task under one root points at the same plan.
"""
from __future__ import annotations

from . import provenance

KINDS = ("new", "continues")


def _plan_exists(conn, plan_id: str) -> bool:
    return conn.execute("SELECT 1 FROM Plans WHERE plan_id = ?", (plan_id,)).fetchone() is not None


def validate(conn, root) -> None:
    """Raise ValueError, naming what is wrong, for a root that cannot be
    recorded. Called before the publish writes anything (FL-216)."""
    if root is None:
        return
    if not isinstance(root, dict):
        raise ValueError(f"root must be an object with a kind, got {type(root).__name__}")
    kind = root.get("kind")
    if kind not in KINDS:
        raise ValueError(f"root.kind must be one of {KINDS}, got {kind!r}")
    if root.get("basis") is not None and not isinstance(root["basis"], str):
        raise ValueError("root.basis must be a sentence")
    if kind == "continues":
        target = root.get("plan_id")
        if not isinstance(target, str) or not target:
            raise ValueError("root.kind continues needs root.plan_id, the plan it continues")
        if not _plan_exists(conn, target):
            raise ValueError(f"root.plan_id: there is no plan {target}")
    elif root.get("utterance_id") is not None:
        uid = root["utterance_id"]
        u = provenance.get_utterance(conn, int(uid)) if isinstance(uid, int) else None
        if u is None:
            raise ValueError(f"root.utterance_id: there is no utterance {uid}")
        if provenance.is_injected_prompt(u["text"]):
            raise ValueError(f"root.utterance_id: utterance {uid} is text Claude Code injected, not the user's words")


def root_of(conn, plan_id: str) -> str | None:
    """The plan where this plan's root started, from its latest row; None when
    the latest row is unknown or rejected, or there is none."""
    r = conn.execute("SELECT kind, root_plan_id, state FROM plan_root WHERE plan_id = ? ORDER BY id DESC LIMIT 1",
                     (plan_id,)).fetchone()
    if r is None or r["state"] == "rejected":
        return None
    return r["root_plan_id"]


def record(conn, *, plan_id: str, root, recorded_by: str = "agent", commit: bool = True) -> dict:
    """Append this plan's root row; {kind, root_plan_id, continues_plan_id}."""
    validate(conn, root)
    if root is None:
        row = {"kind": "unknown", "continues_plan_id": None, "root_plan_id": None, "utterance_id": None,
               "basis": None, "recorded_by": "system"}
    elif root["kind"] == "new":
        row = {"kind": "new", "continues_plan_id": None, "root_plan_id": plan_id,
               "utterance_id": root.get("utterance_id"), "basis": root.get("basis"), "recorded_by": recorded_by}
    else:
        target = root["plan_id"]
        row = {"kind": "continues", "continues_plan_id": target, "root_plan_id": root_of(conn, target) or target,
               "utterance_id": None, "basis": root.get("basis"), "recorded_by": recorded_by}
    conn.execute("INSERT INTO plan_root (plan_id, kind, continues_plan_id, root_plan_id, utterance_id, basis, state, recorded_by) "
                 "VALUES (?, ?, ?, ?, ?, ?, 'asserted', ?)",
                 (plan_id, row["kind"], row["continues_plan_id"], row["root_plan_id"], row["utterance_id"],
                  row["basis"], row["recorded_by"]))
    if commit:
        conn.commit()
    return {k: row[k] for k in ("kind", "root_plan_id", "continues_plan_id")}


_LATEST = ("SELECT r.* FROM plan_root r WHERE r.id = (SELECT MAX(id) FROM plan_root WHERE plan_id = r.plan_id) "
           "AND r.state <> 'rejected'")


def _words(conn, root_plan_id: str) -> str | None:
    """What the root says: the user's sentence the new-root row points at, else
    the plan's user_query, else its goal."""
    r = conn.execute("SELECT utterance_id FROM plan_root WHERE plan_id = ? AND kind = 'new' ORDER BY id DESC LIMIT 1",
                     (root_plan_id,)).fetchone()
    if r and r["utterance_id"] is not None:
        u = provenance.get_utterance(conn, r["utterance_id"])
        if u:
            return u["text"]
    p = conn.execute("SELECT user_query, original_goal FROM Plans WHERE plan_id = ?", (root_plan_id,)).fetchone()
    return (p["user_query"] or p["original_goal"]) if p else None


def recent(conn, project: str, limit: int = 8) -> list[dict]:
    """The project's roots, newest first: {root_plan_id, created_at, words, tasks}."""
    rows = conn.execute(
        f"SELECT l.root_plan_id, COUNT(*) AS tasks, MIN(p0.created_at) AS created_at, MAX(l.id) AS last "
        f"FROM ({_LATEST}) l JOIN Plans p0 ON p0.plan_id = l.root_plan_id "
        f"WHERE l.root_plan_id IS NOT NULL AND p0.project = ? GROUP BY l.root_plan_id "
        f"ORDER BY (SELECT MIN(id) FROM plan_root WHERE plan_id = l.root_plan_id) DESC LIMIT ?",
        (project, limit)).fetchall()
    return [{"root_plan_id": r["root_plan_id"], "created_at": r["created_at"], "tasks": r["tasks"],
             "words": _words(conn, r["root_plan_id"])} for r in rows]


def tasks_under(conn, root_plan_id: str, exclude: str | None = None) -> list[dict]:
    """The plans whose latest root row points at this root, oldest first."""
    rows = conn.execute(
        f"SELECT p.plan_id, p.original_goal AS goal, p.status, p.created_at FROM ({_LATEST}) l "
        f"JOIN Plans p ON p.plan_id = l.plan_id WHERE l.root_plan_id = ? AND p.plan_id IS NOT ? "
        f"ORDER BY p.created_at, l.id", (root_plan_id, exclude)).fetchall()
    return [dict(r) for r in rows]
