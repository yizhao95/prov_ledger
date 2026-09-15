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
    }


def write_baseline(path, data: dict) -> None:
    Path(path).write_text(json.dumps(data, indent=1, sort_keys=True) + "\n", encoding="utf-8")


def read_baseline(path) -> dict | None:
    p = Path(path)
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))
