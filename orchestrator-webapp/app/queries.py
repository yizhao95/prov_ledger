"""Read-only SQLite queries for the orchestrator dashboard.

Opens the orchestrator DB in WAL mode so we can read concurrently while the
orchestrator-cli writes. Never mutates the database.
"""
from __future__ import annotations

import hashlib
import os
import re
import sqlite3
from pathlib import Path

# DB location resolves in this order:
#   1. ORCH_DB environment variable (explicit override)
#   2. ~/skill-workspace/orchestrator.db (default)
DEFAULT_DB_PATH = Path(
    os.environ.get("ORCH_DB", Path.home() / "skill-workspace" / "orchestrator.db")
)


def db_path_display() -> str:
    """The DB path the dashboard is actually reading (footer), ~-abbreviated.

    Was a hardcoded string before, which lied whenever ORCH_DB pointed
    somewhere else (e.g. the silent-class-drop demo DB)."""
    p = str(DEFAULT_DB_PATH)
    home = str(Path.home())
    return "~" + p[len(home):] if p.startswith(home) else p


def open_db_readonly(path: Path | str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Open SQLite read-only with WAL enabled (non-blocking concurrent reads).

    Uses URI mode with mode=ro so we cannot accidentally mutate. WAL pragma
    must still be set on the connection for concurrent-read semantics.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"orchestrator DB not found at {path}")
    uri = f"file:{path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, isolation_level=None)
    conn.row_factory = sqlite3.Row
    # WAL must be set globally by writer; reader just relies on it being on.
    # We can still set busy_timeout so we wait briefly if writer is mid-tx.
    conn.execute("PRAGMA busy_timeout = 2000")
    return conn


def get_latest_plan(conn: sqlite3.Connection) -> dict | None:
    """Return the most relevant plan to display.

    Priority order:
      1. Most recent IN_PROGRESS plan
      2. Most recent PENDING plan
      3. Most recent COMPLETED/FAILED plan (so dashboard never goes blank)
    """
    row = conn.execute(
        """
        SELECT * FROM Plans
        ORDER BY
            CASE status
                WHEN 'IN_PROGRESS' THEN 1
                WHEN 'PENDING'     THEN 2
                WHEN 'COMPLETED'   THEN 3
                WHEN 'FAILED'      THEN 4
                ELSE 5
            END,
            COALESCE(completed_at, created_at) DESC
        LIMIT 1
        """
    ).fetchone()
    return dict(row) if row else None


def get_steps_for_plan(conn: sqlite3.Connection, plan_id: str) -> list[dict]:
    """All steps for a plan, in execution order."""
    rows = conn.execute(
        "SELECT * FROM Steps WHERE plan_id = ? ORDER BY execution_order, step_id",
        (plan_id,),
    ).fetchall()
    return [dict(r) for r in rows]


# ── tree + parallel-detection helpers ─────────────────────────────────────
# Used by the /plan/{plan_id} route to render the tree view (parent-child
# nesting + side-by-side rendering for sibling steps that overlapped in time).

def _intervals_overlap(a_start: str | None, a_end: str | None,
                       b_start: str | None, b_end: str | None) -> bool:
    """Two [start, end] windows overlap?

    None start  -> step never started, can't be parallel with anything.
    None end    -> step still in progress; treat as 'open-ended' (sentinel).

    Timestamps are SQLite ISO strings ('YYYY-MM-DD HH:MM:SS') which compare
    correctly as plain strings — no parsing needed.
    """
    if not a_start or not b_start:
        return False
    OPEN = "9999-99-99 99:99:99"   # sentinel: still running
    a_end_s = a_end or OPEN
    b_end_s = b_end or OPEN
    # Standard half-open interval overlap.
    return a_start < b_end_s and b_start < a_end_s


def detect_parallel_groups(siblings: list[dict]) -> dict[str, int | None]:
    """Cluster sibling steps whose [started_at, completed_at] windows overlap.

    Returns {step_id -> group_id_or_None}. group_id is a stable small int per
    group (1, 2, ...). Steps with no overlapping peer get None.

    Uses union-find via simple iterative grouping (siblings list is small —
    typically <10 — so O(N^2) is fine and avoids a dependency).
    """
    n = len(siblings)
    if n < 2:
        return {s["step_id"]: None for s in siblings}

    # parent[i] = root of the group i belongs to (union-find)
    parent = list(range(n))
    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    def union(i: int, j: int) -> None:
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[ri] = rj

    for i in range(n):
        for j in range(i + 1, n):
            if _intervals_overlap(
                siblings[i].get("started_at"), siblings[i].get("completed_at"),
                siblings[j].get("started_at"), siblings[j].get("completed_at"),
            ):
                union(i, j)

    # Assign group_id per cluster (only clusters with size>1 count as parallel)
    cluster_members: dict[int, list[int]] = {}
    for i in range(n):
        cluster_members.setdefault(find(i), []).append(i)

    result: dict[str, int | None] = {}
    next_group_id = 1
    for members in cluster_members.values():
        if len(members) >= 2:
            for i in members:
                result[siblings[i]["step_id"]] = next_group_id
            next_group_id += 1
        else:
            result[siblings[members[0]]["step_id"]] = None
    return result


def build_step_tree(steps: list[dict]) -> list[dict]:
    """Convert a flat steps list into a nested tree with parallel annotations.

    Each returned dict is the original step dict + two extra keys:
      - children:           list[dict]    nested sub-steps (deviation children)
      - parallel_group_id:  int | None    same int = ran in parallel; None = alone

    Returns the list of root steps (parent_step_id is null OR parent missing).
    """
    by_id = {s["step_id"]: dict(s, children=[], parallel_group_id=None) for s in steps}
    roots: list[dict] = []
    for s in steps:
        node = by_id[s["step_id"]]
        parent_id = s.get("parent_step_id")
        if parent_id and parent_id in by_id:
            by_id[parent_id]["children"].append(node)
        else:
            roots.append(node)   # root or orphan-with-missing-parent

    def annotate(siblings: list[dict]) -> None:
        groups = detect_parallel_groups(siblings)
        for s in siblings:
            s["parallel_group_id"] = groups[s["step_id"]]
            annotate(s["children"])

    annotate(roots)
    return roots


def get_plan_by_id(conn: sqlite3.Connection, plan_id: str) -> dict | None:
    """Look up a specific plan by id (used by /plan/{plan_id} route)."""
    row = conn.execute("SELECT * FROM Plans WHERE plan_id = ?", (plan_id,)).fetchone()
    return dict(row) if row else None


def get_skills_for_plan(conn: sqlite3.Connection, plan_id: str) -> list[dict]:
    """All skill activations for a plan, in activation order.

    Empty list → the plan was created BEFORE the skill-tracking convention
    (or the agent forgot to pass --skill at init-plan time / record-skill mid-flight).
    Template renders this case as a soft "(none recorded)" hint, not an error,
    so legacy plans don't look broken.
    """
    rows = conn.execute(
        "SELECT activation_id, skill_name, source, step_id, reason, activated_at "
        "FROM SkillActivations WHERE plan_id = ? "
        "ORDER BY activated_at, activation_id",
        (plan_id,),
    ).fetchall()
    return [dict(r) for r in rows]


# Max length for the human-friendly card title before we ellipsize.
# Long enough to fit a short sentence, short enough that history rows
# stay scannable and don't wrap onto 3+ lines.
SHORT_TITLE_MAX_CHARS = 70


def short_title(plan: dict | None) -> str:
    """Return a SHORT human-friendly title for a plan card.

    Strategy (in priority order):
      1. If user_query exists, take its FIRST SENTENCE
         (everything up to the first '.', '!', '?', or '\\n').
      2. If that sentence is still > SHORT_TITLE_MAX_CHARS, truncate
         and append an ellipsis.
      3. If user_query is NULL/empty, fall back to plan_id.

    The full original query is shown separately on the plan-detail page
    in a labeled 'Original Query' block — short_title is for cards.
    """
    if not plan:
        return ""
    raw = (plan.get("user_query") or "").strip()
    if not raw:
        return plan.get("plan_id", "")
    # First sentence: stop at . ! ? or newline
    first_sentence = re.split(r"(?<=[.!?])\s|\n", raw, maxsplit=1)[0].strip()
    if not first_sentence:
        first_sentence = raw
    if len(first_sentence) > SHORT_TITLE_MAX_CHARS:
        return first_sentence[: SHORT_TITLE_MAX_CHARS - 1].rstrip() + "…"
    return first_sentence


def list_all_plans(conn: sqlite3.Connection) -> list[dict]:
    """All plans newest-first, with step counts — for the history page.

    Joins step counts in a single query (no N+1) so the history page
    stays fast even with hundreds of plans.
    """
    rows = conn.execute("""
        SELECT p.plan_id, p.user_query, p.original_goal, p.status,
               p.created_at, p.completed_at, p.revision_count, p.max_revisions,
               COUNT(s.step_id)                                               AS total_steps,
               SUM(CASE WHEN s.status = 'COMPLETED' THEN 1 ELSE 0 END)        AS completed_steps
        FROM Plans p
        LEFT JOIN Steps s ON s.plan_id = p.plan_id
        GROUP BY p.plan_id
        ORDER BY p.created_at DESC
    """).fetchall()
    return [dict(r) for r in rows]


# Entry delimiter used by orchestrator/telemetry.py:append_step_log.
# Each append-log invocation joins with this marker.
ENTRY_DELIM = "\n---\n"

# Legacy fallback: lots of older logs were written before the --- delimiter
# existed. Those logs use bracketed-timestamp prefixes like '[17:02]' or
# bare 'HH:MM' to mark entries. We use this regex to split them.
_TIMESTAMP_RE = re.compile(r"^\[?\d{1,2}:\d{2}\]?\s", re.MULTILINE)


def _split_log_entries(log_context: str) -> list[str]:
    """Split a log_context blob into individual entries.

    Strategy:
      1. If '\\n---\\n' separators are present (modern format), split on them.
      2. Otherwise look for timestamp prefixes (legacy format) and split
         before each one.
      3. Otherwise treat the whole blob as 1 entry.

    Empty entries are dropped. Each returned entry has whitespace stripped
    on both ends but internal newlines preserved (so multi-line entries
    keep their formatting).
    """
    if not log_context:
        return []
    if ENTRY_DELIM in log_context:
        return [e.strip() for e in log_context.split(ENTRY_DELIM) if e.strip()]
    # Legacy fallback: split before each timestamp marker
    positions = [m.start() for m in _TIMESTAMP_RE.finditer(log_context)]
    if len(positions) <= 1:
        return [log_context.strip()]
    entries: list[str] = []
    # Capture any prelude before the first timestamp
    if positions[0] > 0:
        prelude = log_context[: positions[0]].strip()
        if prelude:
            entries.append(prelude)
    for i, start in enumerate(positions):
        end = positions[i + 1] if i + 1 < len(positions) else len(log_context)
        chunk = log_context[start:end].rstrip()
        if chunk:
            entries.append(chunk)
    return entries


def count_log_entries(log_context: str) -> int:
    """Total number of distinct entries in a log_context blob."""
    return len(_split_log_entries(log_context))


def get_last_n_log_entries(log_context: str, n: int = 10) -> str:
    """Return the last N entries joined by a blank line for visual clarity.

    For chatty steps this avoids dumping kilobytes of stale log into the
    dashboard — you only see the most recent activity.
    """
    entries = _split_log_entries(log_context)
    if not entries:
        return ""
    tail = entries[-n:] if len(entries) > n else entries
    # Blank line between entries makes visual distinction clear in <pre>
    return "\n\n".join(tail)


def count_completed_steps(steps: list[dict]) -> int:
    return sum(1 for s in steps if s["status"] == "COMPLETED")


def count_total_plans(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) AS n FROM Plans").fetchone()
    return row["n"] if row else 0


def get_db_size_kb(path: Path | str = DEFAULT_DB_PATH) -> int:
    p = Path(path)
    if not p.exists():
        return 0
    return p.stat().st_size // 1024


def compute_etag(conn: sqlite3.Connection) -> str:
    """Cheap content-signature for the dashboard view.

    Returns a stable hex digest that changes IFF something visible on the
    dashboard changed: any plan row, any step row, any log_context append,
    any agent_input/agent_output write, or a status flip.

    Two queries (both index-friendly), then a single SHA256. Result is used
    as an HTTP ETag so HTMX can skip the swap on 304 Not Modified.

    Cheap by design — LENGTH() avoids hashing potentially-large blob fields.
    Two different states will only collide if every field-length AND every
    timestamp matches exactly, which is good enough for a 2s poll loop.
    """
    # Plans signature: row count + every plan's mutable fields
    plan_rows = conn.execute(
        "SELECT plan_id, status, revision_count, created_at, completed_at "
        "FROM Plans ORDER BY plan_id"
    ).fetchall()
    # Steps signature: per-step status, timestamps, and content lengths
    step_rows = conn.execute(
        "SELECT step_id, status, started_at, completed_at, step_type, "
        "       LENGTH(log_context)               AS log_len, "
        "       LENGTH(COALESCE(agent_input,''))  AS in_len, "
        "       LENGTH(COALESCE(agent_output,'')) AS out_len "
        "FROM Steps ORDER BY step_id"
    ).fetchall()
    # SkillActivations signature: row count + per-row identity. Required so the
    # dashboard re-renders within the 2s poll when `record-skill` adds a new row.
    skill_rows = conn.execute(
        "SELECT activation_id, plan_id, skill_name, source, step_id, activated_at "
        "FROM SkillActivations ORDER BY activation_id"
    ).fetchall()
    # Data panel signature (Phase 5.2): profile snapshots + decisions are
    # appended mid-step, so max(id) per table is enough to invalidate. Older
    # DBs without migrations 012/013 just contribute a constant.
    try:
        data_sig = conn.execute(
            "SELECT (SELECT COALESCE(MAX(id), 0) FROM data_profile), "
            "       (SELECT COALESCE(MAX(id), 0) FROM llm_decisions)"
        ).fetchone()
    except sqlite3.Error:
        data_sig = (0, 0)

    h = hashlib.sha256()
    for r in plan_rows:
        h.update(b"P|")
        h.update("|".join(str(v) for v in r).encode("utf-8"))
        h.update(b"\n")
    for r in step_rows:
        h.update(b"S|")
        h.update("|".join(str(v) for v in r).encode("utf-8"))
        h.update(b"\n")
    for r in skill_rows:
        h.update(b"K|")
        h.update("|".join(str(v) for v in r).encode("utf-8"))
        h.update(b"\n")
    h.update(b"D|")
    h.update("|".join(str(v) for v in data_sig).encode("utf-8"))
    h.update(b"\n")
    # Quoted per RFC 7232 §2.3
    return f'"{h.hexdigest()[:16]}"'


# ── Helpers for templating ───────────────────────────────────────────────
# Status colors are RESERVED (good/warning/critical/serious) and always ship
# icon + label. Soft tint chips for calm states; FAILED stays solid — failure
# leads, never whispers.
STATUS_BADGES = {
    "PENDING":      ("⏳", "bg-gray-500/10 text-gray-600"),
    "IN_PROGRESS":  ("🚧", "bg-[#fab219]/15 text-[#7a5200]"),
    "COMPLETED":    ("✅", "bg-[#0ca30c]/10 text-[#006300]"),
    "FAILED":       ("✕",  "bg-brand-red text-white"),
    "NEEDS_REVIEW": ("👀", "bg-[#ec835a]/15 text-[#8a3416]"),
}

# Step type = IDENTITY -> categorical slots in fixed order (validated set,
# worst adjacent CVD dE 47). Rendered as a colored dot beside ink text — the
# text never wears the series color. Third element = the dot's bg class.
TYPE_BADGES = {
    "ANALYSIS":      ("🔍", "bg-[#2a78d6]"),   # slot 1 blue
    "CODE":          ("💻", "bg-[#1baf7a]"),   # slot 2 aqua
    "COMMAND":       ("⚡", "bg-[#eda100]"),   # slot 3 yellow
    "THINKING":      ("🧠", "bg-[#4a3aa7]"),   # slot 5 violet
    "SUB_AGENT":     ("🤖", "bg-[#e87ba4]"),   # slot 7 magenta
    "DOCUMENTATION": ("📝", "bg-[#eb6834]"),   # slot 8 orange
}

# v3: skill-activation source → (icon, label, tailwind classes) for the Skills Activated panel.
# Sources are the four enum values from migration 005_skill_activations.sql.
SOURCE_BADGES = {
    "iron-law":         ("⚖️",  "iron-law",         "bg-[#4a3aa7]"),
    "auto-search":      ("🔍", "auto-search",      "bg-[#2a78d6]"),
    "explicit-mention": ("💬", "explicit-mention", "bg-[#e87ba4]"),
    "deferred-load":    ("⏳", "deferred-load",    "bg-[#eda100]"),
}


def status_badge(status: str) -> tuple[str, str]:
    """Return (emoji, tailwind classes) for a status."""
    return STATUS_BADGES.get(status, ("•", "bg-gray-500/10 text-gray-500"))


def type_badge(step_type: str | None) -> tuple[str, str, str]:
    """Return (icon, label, dot-color class) for a step type. '—' if NULL."""
    if not step_type:
        return ("—", "untyped", "bg-gray-300")
    icon, dot = TYPE_BADGES.get(step_type, ("•", "bg-gray-400"))
    return (icon, step_type.title().replace("_", " "), dot)


def source_badge(source: str | None) -> tuple[str, str, str]:
    """Return (icon, label, tailwind classes) for a SkillActivation source enum.

    Falls back to a neutral gray badge if the source is NULL or an unknown value
    (defensive — the DB CHECK constraint should already prevent unknown values).
    """
    if not source:
        return ("—", "unknown", "bg-gray-300")
    return SOURCE_BADGES.get(source, ("•", source, "bg-gray-400"))


def format_duration(started_at: str | None, completed_at: str | None) -> str:
    """Human-readable duration string."""
    if not started_at:
        return "—"
    if not completed_at:
        return "running…"
    try:
        from datetime import datetime
        s = datetime.fromisoformat(started_at)
        c = datetime.fromisoformat(completed_at)
        delta = c - s
        seconds = int(delta.total_seconds())
        if seconds < 60:
            return f"{seconds}s"
        return f"{seconds // 60}m {seconds % 60}s"
    except (ValueError, TypeError):
        return "—"


def relative_time(ts: str | None) -> str:
    """Human "time ago" for a stored UTC timestamp (DASH-UX5).

    Returns "—" for missing values and the raw string if it can't be parsed,
    so a malformed timestamp never breaks the page.
    """
    if not ts:
        return "—"
    try:
        from datetime import datetime, timezone
        t = datetime.fromisoformat(ts)
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        secs = int((datetime.now(timezone.utc) - t).total_seconds())
        if secs < 0:
            secs = 0
        if secs < 60:
            return f"{secs}s ago"
        if secs < 3600:
            return f"{secs // 60}m ago"
        if secs < 86400:
            return f"{secs // 3600}h ago"
        return f"{secs // 86400}d ago"
    except (ValueError, TypeError):
        return ts


def get_deviations(conn: sqlite3.Connection, plan_id: str) -> list[dict]:
    """Read the deviation/revision history for a plan (DASH-UX4).

    Reads the Deviations table (orchestrator migration 010). Degrades to [] on an
    older DB that predates the table, so the dashboard stays robust + read-only.
    """
    try:
        rows = conn.execute(
            "SELECT deviation_id, target_step_id, justification, revision_count, "
            "created_at FROM Deviations WHERE plan_id = ? ORDER BY deviation_id",
            (plan_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.Error:
        return []


def get_data_profiles(conn: sqlite3.Connection, plan_id: str) -> list[dict]:
    """Latest runtime profile snapshot per dataset for a plan (Phase 5.2).

    Reads the data_profile table (orchestrator migration 012). A "snapshot" is
    one profiling pass over a dataset; the latest one per dataset is the batch
    sharing that dataset's MAX(observed_at). We also report how many snapshots
    exist, so the reader can see a dataset was re-profiled after a fix.
    Degrades to [] on an older DB.
    """
    try:
        rows = conn.execute(
            """SELECT p.dataset, p.step_id, p.observed_at, p.column_name,
                      p.dtype, p.null_frac, p.row_count, p.distinct_count
               FROM data_profile p
               WHERE p.plan_id = ?
                 AND p.observed_at = (
                     SELECT MAX(q.observed_at) FROM data_profile q
                     WHERE q.plan_id = p.plan_id AND q.dataset = p.dataset)
               ORDER BY p.dataset, p.id""",
            (plan_id,),
        ).fetchall()
        counts = dict(conn.execute(
            "SELECT dataset, COUNT(DISTINCT observed_at) FROM data_profile "
            "WHERE plan_id = ? GROUP BY dataset",
            (plan_id,),
        ).fetchall())
    except sqlite3.Error:
        return []

    grouped: dict[str, dict] = {}
    for r in rows:
        g = grouped.setdefault(r["dataset"], {
            "dataset": r["dataset"], "step_id": r["step_id"],
            "observed_at": r["observed_at"], "row_count": r["row_count"],
            "snapshot_count": counts.get(r["dataset"], 1), "columns": {},
        })
        # Same-second re-profile collides on observed_at; later id wins.
        g["columns"][r["column_name"]] = {
            "column_name": r["column_name"], "dtype": r["dtype"],
            "null_frac": r["null_frac"], "distinct_count": r["distinct_count"],
        }
    out = []
    for g in grouped.values():
        g["columns"] = sorted(g["columns"].values(), key=lambda c: c["column_name"])
        out.append(g)
    return out


def get_data_decisions(conn: sqlite3.Connection, plan_id: str) -> list[dict]:
    """LLM data decisions recorded for a plan (Phase 5.2).

    Reads the llm_decisions table (orchestrator migration 013) — the drift →
    decision → outcome trail written by the backbone. Degrades to [] on an
    older DB. Strictly read-only: the dashboard never writes decisions.
    """
    try:
        rows = conn.execute(
            "SELECT id, step_id, dataset, column_name, observed_before, "
            "observed_after, drift_kind, decision, rationale, action, outcome, "
            "human_feedback, created_at FROM llm_decisions "
            "WHERE plan_id = ? ORDER BY id",
            (plan_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.Error:
        return []


# Phase 5.2: outcome → (icon, tailwind classes) for the data-decision trail.
OUTCOME_BADGES = {
    "resolved":   ("✅", "bg-[#0ca30c]/10 text-[#006300]"),
    "unresolved": ("✕",  "bg-brand-red text-white"),
    "halted":     ("🛑", "bg-brand-red text-white"),
    "noop":       ("➖", "bg-gray-500/10 text-gray-600"),
}


def outcome_badge(outcome: str | None) -> tuple[str, str]:
    return OUTCOME_BADGES.get(outcome or "", ("•", "bg-gray-500/10 text-gray-600"))
