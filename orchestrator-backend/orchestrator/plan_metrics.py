"""Per-plan tool-call metrics and the performance baseline (DP phase 0, H1).

A plan's cost is the tool_call_log rows inside its created_at..completed_at
window whose cwd lies in the project's repo (psg_bridge.repo_for); plans
without a project count the window alone. Plans that predate the hook have no
rows: they get a PROXY — Steps.log_context entries per step (the dashboard's
count_log_entries algorithm) — under its own name, never mixed into
calls_per_step. `baseline()` summarises completed plans (median / p90) and
docs/perf-baseline.json is the threshold test_h1_threshold reads.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from . import psg_bridge

ENTRY_DELIM = "\n---\n"
_TIMESTAMP_RE = re.compile(r"^\[?\d{1,2}:\d{2}\]?\s", re.MULTILINE)
_TS = "%Y-%m-%d %H:%M:%S"


def _count_entries(log_context: str | None) -> int:
    """Same algorithm as app.queries.count_log_entries (the backend cannot import the webapp)."""
    if not log_context:
        return 0
    if ENTRY_DELIM in log_context:
        return len([e for e in log_context.split(ENTRY_DELIM) if e.strip()])
    positions = [m.start() for m in _TIMESTAMP_RE.finditer(log_context)]
    if len(positions) <= 1:
        return 1
    n = len(positions)
    if positions[0] > 0 and log_context[:positions[0]].strip():
        n += 1
    return n


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.strptime(ts[:19], _TS)
    except ValueError:
        return None


def _now_str() -> str:
    return datetime.now(timezone.utc).strftime(_TS)


def _in_repo(cwd: str | None, repo: str | None) -> bool:
    if repo is None:
        return True
    if not cwd:
        return False
    repo = repo.rstrip("/")
    return cwd == repo or cwd.startswith(repo + "/")


def calls_for_plan(conn, plan_id: str) -> dict:
    """{plan_id, tool_calls, wall_s, steps, calls_per_step, wall_per_step, measured,
    proxy_log_entries, proxy_calls_per_step, window}. measured is False when no
    tool call fell into the window (a plan from before the hook, or a plan that
    ran in another checkout) — then calls_per_step is None and only the proxy
    speaks."""
    row = conn.execute("SELECT plan_id, project, created_at, completed_at FROM Plans WHERE plan_id = ?", (plan_id,)).fetchone()
    if row is None:
        raise KeyError(f"no plan {plan_id!r}")
    project, created, completed = row[1], row[2], row[3] or _now_str()
    repo = psg_bridge.repo_for(project) if project else None
    rows = conn.execute("SELECT cwd FROM tool_call_log WHERE at >= ? AND at <= ?", (created, completed)).fetchall()
    tool_calls = sum(1 for r in rows if _in_repo(r[0], repo))
    steps_rows = conn.execute("SELECT log_context FROM Steps WHERE plan_id = ? AND COALESCE(is_review, 0) = 0", (plan_id,)).fetchall()
    steps = len(steps_rows)
    proxy = sum(_count_entries(r[0]) for r in steps_rows)
    a, b = _parse(created), _parse(completed)
    wall_s = int((b - a).total_seconds()) if a and b else None
    measured = tool_calls > 0
    return {
        "plan_id": plan_id, "project": project, "window": [created, completed],
        "tool_calls": tool_calls, "wall_s": wall_s, "steps": steps,
        "calls_per_step": round(tool_calls / steps, 4) if measured and steps else None,
        "wall_per_step": round(wall_s / steps, 4) if wall_s is not None and steps else None,
        "measured": measured,
        "proxy_log_entries": proxy,
        "proxy_calls_per_step": round(proxy / steps, 4) if steps else None,
    }


def _median(xs: list[float]) -> float | None:
    if not xs:
        return None
    s = sorted(xs)
    n = len(s)
    return round(s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2, 4)


def _p90(xs: list[float]) -> float | None:
    if not xs:
        return None
    s = sorted(xs)
    pos = 0.9 * (len(s) - 1)
    lo, hi = int(pos), min(int(pos) + 1, len(s) - 1)
    return round(s[lo] + (s[hi] - s[lo]) * (pos - lo), 4)


def _summary(xs: list[float]) -> dict:
    return {"median": _median(xs), "p90": _p90(xs), "n": len(xs)}


def baseline(conn, *, since: str | None = None) -> dict:
    """Median / p90 calls_per_step and wall_per_step over COMPLETED plans
    (created_at >= since when given); plans without a measured call contribute
    to proxy_calls_per_step only."""
    sql = "SELECT plan_id FROM Plans WHERE status = 'COMPLETED'"
    args: list = []
    if since:
        sql += " AND created_at >= ?"
        args.append(since)
    plans = [calls_for_plan(conn, r[0]) for r in conn.execute(sql + " ORDER BY created_at", args)]
    measured = [m for m in plans if m["measured"] and m["calls_per_step"] is not None]
    proxy = [m for m in plans if not m["measured"] and m["proxy_calls_per_step"] is not None]
    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "since": since,
        "n_plans": len(plans), "n_measured": len(measured), "n_proxy": len(proxy),
        "calls_per_step": _summary([m["calls_per_step"] for m in measured]),
        "wall_per_step": _summary([m["wall_per_step"] for m in measured if m["wall_per_step"] is not None]),
        "proxy_calls_per_step": _summary([m["proxy_calls_per_step"] for m in proxy]),
        "plans": {m["plan_id"]: {"calls_per_step": m["calls_per_step"], "tool_calls": m["tool_calls"], "steps": m["steps"]}
                  for m in measured},
        # DP phase 2 (Task 6): what part of a plan is provledger itself
        "overhead": overhead_baseline(conn, since=since),
    }


def write_baseline(path, data: dict) -> None:
    """Rewrite the baseline file, carrying forward the hand-measured
    `superpowers_only` section (it is not computed here — see docs)."""
    prev = read_baseline(path) or {}
    if "superpowers_only" in prev and "superpowers_only" not in data:
        data = {**data, "superpowers_only": prev["superpowers_only"]}
    Path(path).write_text(json.dumps(data, indent=1, sort_keys=True) + "\n", encoding="utf-8")


def read_baseline(path) -> dict | None:
    p = Path(path)
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


# ── DP phase 2 (Task 6): how much of a plan is provledger itself ─────────────

ORCHESTRATION_RE = re.compile(r"run-step|publish-plan|review_run|complete-step|start-step|deviate|fail-step|finish-plan|record-|ledger-")
PROVENANCE_RE = re.compile(r"reason-|provledger |hooks/|analyzer ")
COMMAND_HEAD_CHARS = 80


def classify(command_head: str | None) -> str | None:
    """orchestration | provenance | None (the agent's own work). A head that
    matches both buckets is provenance (reason-fill.sh runs through the
    executing-plans scripts, but it is provenance work)."""
    if not command_head:
        return None
    if PROVENANCE_RE.search(command_head):
        return "provenance"
    if ORCHESTRATION_RE.search(command_head):
        return "orchestration"
    return None


def overhead(conn, plan_id: str) -> dict:
    """{plan_id, total_calls, orchestration_calls, provenance_calls, orchestration_ratio,
    provenance_ratio, overhead_ratio, context_overhead_tokens, context: {pack, headline,
    injected}, measured}. Calls are the plan window's Bash rows (command_head is
    only recorded for Bash); context is the pack's approx_tokens + the headline
    text / 4 + every PreToolUse injection in the window / 4."""
    row = conn.execute("SELECT project, created_at, completed_at, impact_context, headline_json FROM Plans WHERE plan_id = ?", (plan_id,)).fetchone()
    if row is None:
        raise KeyError(f"no plan {plan_id!r}")
    project, created, completed, ic_json, hl_json = row[0], row[1], row[2] or _now_str(), row[3], row[4]
    repo = psg_bridge.repo_for(project) if project else None
    rows = conn.execute("SELECT cwd, tool_name, command_head FROM tool_call_log WHERE at >= ? AND at <= ?", (created, completed)).fetchall()
    rows = [r for r in rows if _in_repo(r[0], repo)]
    total = len(rows)
    buckets = {"orchestration": 0, "provenance": 0}
    for _, tool, head in rows:
        k = classify(head) if tool == "Bash" else None
        if k:
            buckets[k] += 1
    pack_tokens = 0
    try:
        pack = (json.loads(ic_json) if ic_json else {}).get("pack") or {}
        pack_tokens = int(pack.get("approx_tokens") or 0)
    except (ValueError, TypeError, AttributeError):
        pack_tokens = 0
    headline_tokens = 0
    try:
        doc = json.loads(hl_json) if hl_json else None
        if doc:
            from . import checks
            headline_tokens = len(checks.render(doc)) // 4
    except (ValueError, TypeError, KeyError):
        headline_tokens = 0
    injected = conn.execute("SELECT COALESCE(SUM(injected_chars), 0) FROM read_hit WHERE moment = 'edit' AND at >= ? AND at <= ? "
                            "AND (plan_id = ? OR plan_id IS NULL) AND project IS ?", (created, completed, plan_id, project)).fetchone()[0] or 0
    injected_tokens = int(injected) // 4
    o_ratio = round(buckets["orchestration"] / total, 4) if total else None
    p_ratio = round(buckets["provenance"] / total, 4) if total else None
    return {
        "plan_id": plan_id, "project": project, "window": [created, completed],
        "total_calls": total, "orchestration_calls": buckets["orchestration"], "provenance_calls": buckets["provenance"],
        "orchestration_ratio": o_ratio, "provenance_ratio": p_ratio,
        "overhead_ratio": round(o_ratio + p_ratio, 4) if total else None,
        "context_overhead_tokens": pack_tokens + headline_tokens + injected_tokens,
        "context": {"pack": pack_tokens, "headline": headline_tokens, "injected": injected_tokens},
        "measured": total > 0,
    }


def overhead_baseline(conn, *, since: str | None = None, last: int | None = None) -> dict:
    """median / p90 of overhead_ratio and context_overhead_tokens over COMPLETED
    plans (measured ones for the ratio; every plan for the context number)."""
    sql = "SELECT plan_id FROM Plans WHERE status = 'COMPLETED'"
    args: list = []
    if since:
        sql += " AND created_at >= ?"
        args.append(since)
    sql += " ORDER BY created_at DESC"
    if last:
        sql += " LIMIT ?"
        args.append(int(last))
    plans = [overhead(conn, r[0]) for r in conn.execute(sql, args)]
    measured = [m for m in plans if m["measured"]]
    return {
        "n_plans": len(plans), "n_measured": len(measured),
        "overhead_ratio": _summary([m["overhead_ratio"] for m in measured]),
        "provenance_ratio": _summary([m["provenance_ratio"] for m in measured]),
        "context_overhead_tokens": _summary([float(m["context_overhead_tokens"]) for m in plans]),
        "plans": {m["plan_id"]: {"overhead_ratio": m["overhead_ratio"], "provenance_ratio": m["provenance_ratio"],
                                 "context_overhead_tokens": m["context_overhead_tokens"], "total_calls": m["total_calls"]} for m in plans},
    }
