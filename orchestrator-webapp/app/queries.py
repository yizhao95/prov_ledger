"""Read-only SQLite queries for the orchestrator dashboard.

Opens the orchestrator DB in WAL mode so we can read concurrently while the
orchestrator-cli writes. Never mutates the database.
"""
from __future__ import annotations

import hashlib
import os
import re
import json
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
    somewhere else (e.g. the phantom-uplift demo DB)."""
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
    # Phase 3: reasons (node_reason) and outcomes are appended at close time —
    # max(id) per table invalidates the etag; older DBs contribute a constant.
    try:
        reason_sig = conn.execute(
            "SELECT (SELECT COALESCE(MAX(id), 0) FROM node_reason), "
            "       (SELECT COALESCE(MAX(id), 0) FROM outcomes)"
        ).fetchone()
    except sqlite3.Error:
        reason_sig = (0, 0)
    h.update(b"R|")
    h.update("|".join(str(v) for v in reason_sig).encode("utf-8"))
    h.update(b"\n")
    # DP phase 2 (Task 7): a headline, a response, a shown record or an adopted one
    # are close-time / plan-time rows the page shows — max(id) each; older DBs
    # contribute a constant.
    try:
        dp_sig = conn.execute(
            "SELECT (SELECT COALESCE(MAX(id), 0) FROM headline), "
            "       (SELECT COALESCE(MAX(id), 0) FROM headline_response), "
            "       (SELECT COALESCE(MAX(id), 0) FROM read_hit), "
            "       (SELECT COALESCE(MAX(id), 0) FROM influence), "
            "       (SELECT COALESCE(MAX(id), 0) FROM change_reason)"
        ).fetchone()
    except sqlite3.Error:
        dp_sig = (0, 0, 0, 0, 0)
    h.update(b"H|")
    h.update("|".join(str(v) for v in dp_sig).encode("utf-8"))
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


# ── Phase 3: tier badges + reasons (read-only) ──────────────────────────────
# The LABEL is the differentiator (E3-1): every tier reads as its own word, the
# tint only reinforces it. `unstated` is the display tier of a reason row whose
# text is NULL — an explicit gap, shown as such, never blended into the rest.
# DP phase 2d (Task 1): the five rows come from app/static/tokens.json, the same
# file the Jinja chrome and the React library are generated from — a palette kept
# by hand in two languages drifts, and a drifting tier colour is a drifting claim.
TOKENS_PATH = Path(__file__).resolve().parent / "static" / "tokens.json"


def _load_tokens() -> dict:
    """Read the token file once at import. A missing or broken file is not a
    reason to render a blank dashboard, so the five tiers keep a literal
    fallback — and say, in the returned dict, that it is one."""
    try:
        doc = json.loads(TOKENS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"tiers": {t: {"label": t, "classes": "bg-brand-red/10 text-brand-red"}
                          for t in ("observed", "derived", "asserted", "stated", "unstated")},
                "colors": {}, "severities": {}, "degraded": True}
    doc["degraded"] = False
    return doc


TOKENS = _load_tokens()
TIER_BADGES = {tier: (row["label"], row["classes"]) for tier, row in TOKENS["tiers"].items()}


def tier_badge(tier: str | None) -> tuple[str, str]:
    return TIER_BADGES.get(tier or "unstated", TIER_BADGES["unstated"])


# 来源等级 (source level) — the computed evidence_level of change_reason_v, shown as words
SOURCE_LEVELS = {"linked": "来源等级 linked · 有可核对的引用", "verbal": "来源等级 verbal · 原话或口头来源",
                 "task_context": "来源等级 task_context · 仅任务脉络", "unstated": "来源等级 unstated · 未说明"}


def get_node_reasons(conn: sqlite3.Connection, plan_id: str) -> list[dict]:
    """node_reason rows of a plan (migration 014) + display_tier ('unstated'
    when text is NULL). Degrades to [] on an older DB. Read-only."""
    try:
        rows = conn.execute(
            "SELECT r.id, r.node_key, r.run_id, r.step_id, r.role AS kind, "
            "       COALESCE(r.interpretation, r.statement, substr(u.text, r.verbatim_start + 1, r.verbatim_end - r.verbatim_start)) AS text, "
            "       r.recorded_by AS source, r.tier, r.recorded_at AS created_at, r.evidence_level, r.rule_id "
            "FROM change_reason_v r LEFT JOIN utterance u ON u.id = r.verbatim_utterance_id "
            "WHERE r.plan_id = ? ORDER BY r.id", (plan_id,)).fetchall()
    except sqlite3.Error:
        return []
    out = []
    for r in rows:
        d = dict(r)
        d["display_tier"] = d["tier"]                     # DP phase 1: unstated is a real tier now
        d["source_level"] = SOURCE_LEVELS.get(d.get("evidence_level") or "", d.get("evidence_level") or "—")
        out.append(d)
    return out


def get_outcomes(conn: sqlite3.Connection, plan_id: str) -> list[dict]:
    """The plan's expectations with every outcome recorded against them
    (migration 014 / phase 7 channels), oldest first; an expectation with no
    outcome yet is one row with kind 'pending'. Adds `delta_pct` (metric
    channel) and a one-line `summary`. Read-only; [] on an older DB."""
    try:
        rows = conn.execute(
            "SELECT e.id AS expectation_id, e.target, e.target_kind, e.claim, e.channel, e.created_at AS claimed_at, "
            "       o.id AS outcome_id, o.kind, o.value_json, o.source, o.tier, o.reason, o.backfilled_by_plan, o.observed_at "
            "FROM expectations e LEFT JOIN outcomes o ON o.expectation_id = e.id "
            "WHERE e.plan_id = ? ORDER BY e.id, o.id", (plan_id,)).fetchall()
    except sqlite3.Error:
        return []
    out = []
    for r in rows:
        d = dict(r)
        try:
            value = json.loads(d["value_json"]) if d["value_json"] else {}
        except ValueError:
            value = {}
        if d["kind"] is None:
            d["kind"], d["tier"], d["source"] = "pending", "none", "—"
        d["value"] = value
        d["delta_pct"] = value.get("delta_pct") if d["kind"] == "observed" and isinstance(value, dict) else None
        d["summary"] = _outcome_summary(d["kind"], value, d.get("reason"))
        out.append(d)
    return out


def _outcome_summary(kind: str, value, reason: str | None) -> str:
    if kind == "pending":
        return "no outcome recorded yet — the next reviewed close of another plan backfills it"
    if kind == "none_available":
        return reason or "nothing to observe"
    if not isinstance(value, dict):
        return str(value)
    if "before" in value and "after" in value:                     # metric channel
        pct = value.get("delta_pct")
        unit = f" {value['unit']}" if value.get("unit") else ""
        pct_s = f" ({pct:+.1f}%)" if isinstance(pct, (int, float)) else ""
        return f"{value['before']}{unit} → {value['after']}{unit}{pct_s}"
    if "kinds" in value:                                           # profile_drift channel
        return ("drift: " + ", ".join(value["kinds"])) if value["kinds"] else "no drift between the two snapshots"
    if "signal" in value:                                          # survival
        plans = value.get("plans") or value.get("changed_by") or []
        return f"{value['signal']}" + (f" by {', '.join(plans)}" if plans else "")
    return json.dumps(value, sort_keys=True)[:160]


def get_unstated(conn: sqlite3.Connection, plan_id: str) -> dict:
    """{slots, unstated, pct}: reason slots of the plan and how many stayed unstated."""
    stated: dict[str, bool] = {}
    for r in get_node_reasons(conn, plan_id):
        if r["kind"] != "reason" or not r["node_key"]:
            continue
        stated[r["node_key"]] = stated.get(r["node_key"], False) or (r["tier"] != "unstated")   # DP phase 1: by tier
    slots = len(stated)
    unstated = sum(1 for v in stated.values() if not v)
    return {"slots": slots, "unstated": unstated, "pct": int(100 * unstated / slots) if slots else 0}


# ── Phase 8 Task 3: every claim with its latest outcome, across plans (FL-042) ──
def get_expectations_with_latest_outcome(conn: sqlite3.Connection, project: str | None = None,
                                         limit: int = 200) -> list[dict]:
    """One row per expectation — the claim, its channel, the owning plan's
    title, and the LATEST outcome recorded against it (kind / tier / value
    summary / backfilled_by_plan / observed_at); an expectation with no
    outcome yet is kind 'pending', tier 'pending'. Newest claims first.
    Read-only; [] on a DB that predates migration 014."""
    sql = ("SELECT e.id AS expectation_id, e.plan_id, e.step_id, e.project, e.target, e.target_kind, e.claim, e.channel, "
           "       e.created_at AS claimed_at, p.original_goal, p.user_query, p.status AS plan_status, "
           "       o.id AS outcome_id, o.kind, o.value_json, o.source, o.tier, o.reason, o.backfilled_by_plan, o.observed_at "
           "FROM expectations e "
           "LEFT JOIN Plans p ON p.plan_id = e.plan_id "
           "LEFT JOIN outcomes o ON o.id = (SELECT id FROM outcomes WHERE expectation_id = e.id ORDER BY observed_at DESC, id DESC LIMIT 1) ")
    params: list = []
    if project:
        sql += "WHERE e.project = ? "
        params.append(project)
    sql += "ORDER BY e.created_at DESC, e.id DESC LIMIT ?"
    params.append(limit)
    try:
        rows = conn.execute(sql, params).fetchall()
    except sqlite3.Error:
        return []
    out = []
    for r in rows:
        d = dict(r)
        try:
            value = json.loads(d["value_json"]) if d["value_json"] else {}
        except ValueError:
            value = {}
        if d["kind"] is None:
            d["kind"], d["tier"], d["source"] = "pending", "pending", "—"
        # title: the user's query first, else the plan's goal, else the id (short_title only knows user_query)
        d["plan_title"] = short_title({"plan_id": d["plan_id"], "user_query": d.get("user_query") or d.get("original_goal")})
        d["latest"] = {"kind": d["kind"], "tier": d["tier"], "value": value, "source": d["source"], "reason": d.get("reason"),
                       "backfilled_by_plan": d.get("backfilled_by_plan"), "observed_at": d.get("observed_at"),
                       "delta_pct": value.get("delta_pct") if d["kind"] == "observed" and isinstance(value, dict) else None,
                       "signal": value.get("signal") if isinstance(value, dict) else None,
                       "summary": _outcome_summary(d["kind"], value, d.get("reason"))}
        out.append(d)
    return out


def outcome_stats(rows: list[dict]) -> dict:
    """Counts by latest kind + the share of claims still pending (unstated_ratio
    of the outcome side: claims nobody has observed yet)."""
    counts = {"observed": 0, "survival": 0, "none_available": 0, "pending": 0}
    for r in rows:
        counts[r["kind"]] = counts.get(r["kind"], 0) + 1
    n = len(rows)
    return {"n": n, **counts, "pending_ratio": round(counts["pending"] / n, 4) if n else 0.0,
            "projects": sorted({r["project"] for r in rows if r.get("project")})}


# ── Phase 8 Task 4 (FL-009): a node's ledger — space, time and intent in one query ──
try:                                   # the unified venv has provledger; degrade visibly otherwise
    from provledger import psg_bridge as _psg
except Exception:                      # pragma: no cover — "state graph unavailable" on the page
    _psg = None


# ── DP phase 2d (Task 3b): one timeline of significant moments ───────────────
# A real node carries hundreds of events and most of them are `node_matched` —
# "the analyser recognised this again", true and silent. The timeline keeps the
# moments where something actually changed and FOLDS the rest behind a count:
# a count is the difference between "nothing happened" and "we stopped showing
# you", and only one of those is honest.
SIGNIFICANT_EVENTS = ("node_added", "node_changed", "node_renamed", "node_moved",
                      "column_dropped", "node_removed", "removed", "identity_asserted")
QUIET_EVENTS = ("node_matched", "identity_kept")
SHOW_TIERS = ("observed", "derived", "asserted", "stated", "unstated")
SHOW_SINCE = {"7d": 7, "30d": 30}


def parse_show(show: str | None) -> dict:
    """The chip state, from `?show=`. Comma-separated; `key:value` narrows,
    a bare word is a flag. Unknown keys and values are IGNORED rather than
    emptying the page — a filter nobody can spell should not look like a node
    with no history."""
    state = {"all": False, "adopted": False, "events": [], "tier": [], "since": None, "raw": show or ""}
    for part in (show or "").split(","):
        part = part.strip()
        if not part:
            continue
        if part == "all":
            state["all"] = True
        elif part == "adopted":
            state["adopted"] = True
        elif part.startswith("events:"):
            state["events"] += [e for e in part[7:].split("|") if e]
        elif part.startswith("tier:"):
            state["tier"] += [t for t in part[5:].split("|") if t in SHOW_TIERS]
        elif part.startswith("since:") and part[6:] in SHOW_SINCE:
            state["since"] = part[6:]
    return state


def _within(ts: str | None, since: str | None) -> bool:
    if not since or not ts:
        return True
    from datetime import datetime, timedelta, timezone
    try:
        when = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return True
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when >= datetime.now(timezone.utc) - timedelta(days=SHOW_SINCE[since])


def _timeline_rows(base: dict) -> list[dict]:
    """Events, reasons, rejected paths and constraints merged into one rail,
    newest first — layout spec 2: one row per moment, on one line."""
    rows: list[dict] = []
    for run in base["runs"]:
        for e in run["events"]:
            rows.append({"kind": "event", "event_type": e["event_type"], "tier": e["tier"],
                         "run_id": run["run_id"], "plan_id": run.get("plan_id"), "step_id": run.get("step_id"),
                         "commit_sha": run.get("commit_sha"), "at": e.get("created_at"),
                         "payload": e.get("payload") or {}, "significant": e["event_type"] in SIGNIFICANT_EVENTS})
    for r in base["reasons"]:
        rows.append({"kind": "reason", "event_type": None, "tier": r.get("display_tier") or r.get("tier"),
                     "run_id": r.get("run_id"), "plan_id": r.get("plan_id"), "step_id": r.get("step_id"),
                     "at": r.get("created_at"), "record": r,
                     "significant": (r.get("significance_eff") or "major") != "minor"})
    for c in base["constraints"]:
        rows.append({"kind": "constraint", "event_type": None, "tier": c.get("tier"), "run_id": None,
                     "plan_id": c.get("plan_id"), "at": c.get("created_at"), "record": c, "significant": True})
    rows.sort(key=lambda r: (str(r.get("at") or ""), r.get("run_id") or 0), reverse=True)
    return rows


def merge_decisions(rows: list[dict], hits: dict[int, int] | None = None) -> list[dict]:
    """The rail: one row per CHANGE, one row per DECISION, and a decision appears
    once no matter how often it was activated.

    A constraint being in force, or a reason having been given, is a fact about
    the node. Every later plan that meets it ACTIVATES it — and an activation is
    a hit, not another line of text. Rules no longer write a second record
    (triggers._existing_rule_reason), but 16 identical rows already exist on this
    repo's own compute_etag, so the merge also happens here on read: rows with
    the same (role, text, tier) become one, and each merged row counts as an
    activation because that is what it was. The merge is reported on the row, not
    hidden: `merged` and the plans are part of the result."""
    hits = hits or {}
    out: list[dict] = []
    seen: dict[tuple, dict] = {}
    for r in rows:
        rec = r.get("record") or {}
        text = (rec.get("text") or rec.get("statement") or "").strip()
        if r.get("kind") == "event" or not text:
            out.append({**r, "row": "change", "text": text, "hits": 0, "merged": 0, "plans": []})
            continue
        sig = (r.get("kind"), r.get("tier"), text)
        if sig in seen:
            g = seen[sig]
            g["merged"] += 1
            # N identical writes are N activations — including the first one, which
            # only becomes "a write among many" once a second arrives
            g["hits"] += 2 if g["merged"] == 1 else 1
            if r.get("plan_id"):
                g["plans"] = sorted(set(g["plans"] + [r["plan_id"]]))
            g["first_at"] = min(x for x in (g["first_at"], r.get("at")) if x)
            continue
        stats = rec.get("stats") or {}
        base_hits = hits.get(rec.get("id")) if rec.get("id") in hits else \
            (stats.get("plan") or 0) + (stats.get("edit") or 0) + (stats.get("why") or 0) + (stats.get("close") or 0)
        g = {**r, "row": "decision", "text": text, "hits": int(base_hits or 0), "merged": 0,
             "plans": [r["plan_id"]] if r.get("plan_id") else [], "first_at": r.get("at")}
        seen[sig] = g
        out.append(g)
    return out


def filter_timeline(rows: list[dict], show: dict) -> tuple[list[dict], dict]:
    """(kept, folded counts). Folding is always reported per reason it folded."""
    folded = {"events": 0, "minor": 0, "filtered": 0}
    kept = []
    for r in rows:
        if not show["all"]:
            if r["kind"] == "event" and r["event_type"] in QUIET_EVENTS:
                folded["events"] += 1
                continue
            if not r["significant"]:
                folded["minor"] += 1
                continue
        if show["events"] and r.get("event_type") not in show["events"]:
            folded["filtered"] += 1
            continue
        if show["tier"] and r.get("tier") not in show["tier"]:
            folded["filtered"] += 1
            continue
        if show["since"] and not _within(r.get("at"), show["since"]):
            folded["filtered"] += 1
            continue
        if show["adopted"]:
            stats = (r.get("record") or {}).get("stats") or {}
            if not stats.get("adopted"):
                folded["filtered"] += 1
                continue
        kept.append(r)
    return kept, folded


def get_node_ledger(conn: sqlite3.Connection, project: str, qualified_name: str,
                    significant_only: bool = True, filters: dict | None = None) -> dict:
    """Everything the dashboard shows about one node, read in one go:
    space (the consistency card: callers / output_consumers), time (every
    history event grouped by analysis run, with plan / tier), and intent (the
    close-time reasons recorded against its node_key, and the active
    constraints anchored on it — a restricted constraint keeps its rationale
    and shows only why_ref). PSG is reached only through provledger.psg_bridge
    (mode=ro); a missing graph is a visible `available: False`, never a 500.
    `qualified_name` may also be a node_key (nk_…)."""
    base = {"project": project, "qualified_name": qualified_name, "node_key": None, "available": False, "found": False,
            "reason": None, "runs": [], "events": 0, "card": {}, "reasons": [], "constraints": [], "approx_tokens": 0}
    if _psg is None:
        base["reason"] = "state graph unavailable: provledger.psg_bridge cannot be imported in this environment"
        return base
    db_path = _psg.db_path_for(project)
    if not db_path or not os.path.exists(db_path):
        base["reason"] = f"state graph unavailable: project {project!r} is not registered or its graph file is missing"
        return base
    base["available"] = True
    if qualified_name.startswith("nk_"):
        node_key = qualified_name
        qn = _psg.latest_qualified_name(db_path, node_key) or qualified_name
    else:
        qn = qualified_name
        node_key = _psg.node_key_of(db_path, qualified_name)
    if not node_key:
        base["reason"] = f"{qualified_name} is not in the state graph of {project} (no snapshot carries this name)"
        return base
    base.update(found=True, node_key=node_key, qualified_name=qn)
    # time
    runs: dict[int, dict] = {}
    events = _psg.events_of(db_path, node_key)
    for e in events:
        g = runs.setdefault(e["run_id"], {"run_id": e["run_id"], "commit_sha": e["commit_sha"], "plan_id": e["plan_id"],
                                          "step_id": e["step_id"], "trigger": e["trigger"], "events": []})
        g["events"].append(e)
    base["runs"] = [runs[k] for k in sorted(runs)]
    base["events"] = len(events)
    # space
    base["card"] = _psg.card_of(db_path, qn)
    # intent: reasons (all plans) + constraints anchored by node_key or qualified name
    try:
        rows = conn.execute("SELECT r.id, r.node_key, r.run_id, r.plan_id, r.step_id, r.role AS kind, "
                            "       COALESCE(r.interpretation, r.statement, substr(u.text, r.verbatim_start + 1, r.verbatim_end - r.verbatim_start)) AS text, "
                            "       r.recorded_by AS source, r.tier, r.recorded_at AS created_at, r.evidence_level, r.rule_id "
                            "FROM change_reason_v r LEFT JOIN utterance u ON u.id = r.verbatim_utterance_id "
                            "WHERE r.node_key = ? AND r.role <> 'constraint' ORDER BY r.id", (node_key,)).fetchall()
        base["reasons"] = [dict(r, display_tier=r["tier"], source_level=SOURCE_LEVELS.get(r["evidence_level"] or "", "—")) for r in rows]
    except sqlite3.Error:
        base["reasons"] = []
    # DP phase 2 (Task 7): 展示 per moment (never summed) + 采用 with the adopting plans, per record
    stats = record_stats(conn, [r["id"] for r in base["reasons"]])
    for r in base["reasons"]:
        r["stats"] = stats.get(r["id"])
    try:
        # DP phase 1 (Task 7, closes FL-046): constraints come from change_reason(role=constraint)
        # through the same rule as constraints.anchored_constraints — a personal rationale never leaves
        rows = conn.execute(
            "SELECT r.id, r.statement, r.rationale, r.rationale_visibility, r.state, r.recorded_at AS created_at, r.evidence_level, "
            "       (SELECT f.label FROM reference_link l JOIN reference f ON f.id = l.reference_id WHERE l.reason_id = r.id ORDER BY f.id LIMIT 1) AS why_ref "
            "FROM change_reason_v r WHERE r.project = ? AND r.role = 'constraint' AND r.state = 'active' AND r.superseded_by IS NULL "
            "AND r.rule_id IS NULL AND r.node_key IN (?, ?) ORDER BY r.id", (project, node_key, qn)).fetchall()
        cons, seen = [], set()
        for r in rows:
            d = dict(r)
            if d["statement"] in seen:
                continue
            seen.add(d["statement"])
            d["why_visibility"] = "restricted" if d.get("rationale_visibility") == "personal" else "shared"
            if d["why_visibility"] == "restricted":
                d["rationale"] = None
            d["source_level"] = SOURCE_LEVELS.get(d.get("evidence_level") or "", "—")
            cons.append(d)
        base["constraints"] = cons
    except sqlite3.Error:
        base["constraints"] = []
    stats = record_stats(conn, [c["id"] for c in base["constraints"]])
    for c in base["constraints"]:
        c["stats"] = stats.get(c["id"])
    # DP phase 2d (Task 3b): one rail instead of three columns
    show = dict(filters or parse_show(None))
    if not significant_only:
        show["all"] = True
    rows = _timeline_rows(base)
    base["timeline_all"] = rows
    base["timeline"], base["folded"] = filter_timeline(rows, show)
    base["rail"] = merge_decisions(base["timeline"], _read_hit_counts(conn, base))
    base["show"] = show
    base["upstream"] = list(base["card"].get("callers") or [])
    base["downstream"] = list(base["card"].get("output_consumers") or [])
    base["approx_tokens"] = len(json.dumps(base, default=str)) // 4
    return base


# ── DP phase 2 (Task 7): headline, shown / adopted, hit counts, overhead ──────

try:
    from provledger import plan_metrics as _pm
except Exception:  # pragma: no cover — the dashboard still renders without the backend package
    _pm = None


def get_headline(conn: sqlite3.Connection, plan_id: str) -> dict | None:
    """The plan's latest headline (headline + headline_response, read-only):
    findings with their response or 未回答, an `agent_proceeded` flag, and the
    summary recomputed from the responses. None on an older DB or no row."""
    try:
        row = conn.execute("SELECT id, findings_json, computed_at FROM headline WHERE plan_id = ? ORDER BY id DESC LIMIT 1", (plan_id,)).fetchone()
        if not row:
            return None
        doc = json.loads(row["findings_json"])
        resp = {r["finding_id"]: dict(r) for r in conn.execute(
            "SELECT finding_id, action, rationale, by, cites_json, at FROM headline_response WHERE headline_id = ?", (row["id"],))}
    except (sqlite3.Error, ValueError):
        return None
    findings = doc.get("findings") or []
    for f in findings:
        r = resp.get(f.get("id"))
        f["response"] = r
        f["agent_proceeded"] = bool(r and r["action"] == "proceed" and r["by"] == "agent")
        f["unanswered"] = f.get("severity") == "blocking" and r is None
    s = dict(doc.get("summary") or {})
    s["unanswered"] = sum(1 for f in findings if f["unanswered"])
    s["proceeded_by_agent"] = sum(1 for f in findings if f["agent_proceeded"])
    s["answered"] = sum(1 for f in findings if f["response"])
    return {"headline_id": row["id"], "computed_at": row["computed_at"], "findings": findings, "summary": s, "hints": doc.get("hints") or []}


def _reason_text_map(conn: sqlite3.Connection, ids: list[int]) -> dict[int, dict]:
    if not ids:
        return {}
    ph = ",".join("?" * len(ids))
    try:
        rows = conn.execute(
            f"SELECT r.id, r.node_key, r.role, r.tier, "
            f"       COALESCE(r.interpretation, r.statement, substr(u.text, r.verbatim_start + 1, r.verbatim_end - r.verbatim_start)) AS text "
            f"FROM change_reason_v r LEFT JOIN utterance u ON u.id = r.verbatim_utterance_id WHERE r.id IN ({ph})", ids).fetchall()
    except sqlite3.Error:
        return {}
    return {r["id"]: dict(r) for r in rows}


def get_shown_adopted(conn: sqlite3.Connection, plan_id: str) -> dict:
    """{"plan": {shown, adopted}, "steps": {step_id: {shown, adopted}}} — read_hit
    rows (展示过) and influence rows (采用了) of the plan, per step; rows without
    a step land in the plan bucket. Each entry carries the record's node_key and
    text so the template can link /node/<project>/<key>?at=<id>. Never derived
    from each other (I11); empty on an older DB."""
    out = {"plan": {"shown": [], "adopted": []}, "steps": {}}
    try:
        hits = conn.execute("SELECT reason_id, step_id, moment, at FROM read_hit WHERE plan_id = ? ORDER BY id", (plan_id,)).fetchall()
        infl = conn.execute("SELECT reason_id, step_id, via, by, at FROM influence WHERE plan_id = ? ORDER BY id", (plan_id,)).fetchall()
    except sqlite3.Error:
        return out
    texts = _reason_text_map(conn, sorted({r["reason_id"] for r in hits} | {r["reason_id"] for r in infl}))

    def bucket(step_id):
        if not step_id:
            return out["plan"]
        return out["steps"].setdefault(step_id, {"shown": [], "adopted": []})
    for r in hits:
        t = texts.get(r["reason_id"], {})
        bucket(r["step_id"])["shown"].append({"reason_id": r["reason_id"], "moment": r["moment"], "at": r["at"],
                                             "node_key": t.get("node_key"), "text": (t.get("text") or "")[:120], "role": t.get("role")})
    for r in infl:
        t = texts.get(r["reason_id"], {})
        bucket(r["step_id"])["adopted"].append({"reason_id": r["reason_id"], "via": r["via"], "by": r["by"], "at": r["at"],
                                               "node_key": t.get("node_key"), "text": (t.get("text") or "")[:120], "role": t.get("role")})
    return out


def get_overhead(conn: sqlite3.Connection, plan_id: str) -> dict | None:
    """plan_metrics.overhead through the provledger package (None when it is not
    importable or the plan predates the columns) — the footer's two numbers."""
    if _pm is None:
        return None
    try:
        return _pm.overhead(conn, plan_id)
    except Exception:
        return None


def record_stats(conn: sqlite3.Connection, reason_ids: list[int]) -> dict[int, dict]:
    """reason_stats_v per record — shown per moment (never summed across moments)
    and adopted, plus the plans that adopted it. Empty on an older DB."""
    if not reason_ids:
        return {}
    ph = ",".join("?" * len(reason_ids))
    out: dict[int, dict] = {}
    try:
        for r in conn.execute(f"SELECT reason_id, shown_plan, shown_edit, shown_close, shown_why, adopted FROM reason_stats_v WHERE reason_id IN ({ph})", reason_ids):
            out[r["reason_id"]] = {"plan": r["shown_plan"], "edit": r["shown_edit"], "close": r["shown_close"], "why": r["shown_why"],
                                   "adopted": r["adopted"], "adopted_by": []}
        for r in conn.execute(f"SELECT DISTINCT reason_id, plan_id, session_id FROM influence WHERE reason_id IN ({ph}) ORDER BY id", reason_ids):
            if r["reason_id"] in out:
                out[r["reason_id"]]["adopted_by"].append(r["plan_id"] or (f"session {r['session_id']}" if r["session_id"] else "?"))
    except sqlite3.Error:
        return {}
    return out


# ── DP phase 2b (Task 3): the Graph view ───────────────────────────────────────

def node_badges(conn: sqlite3.Connection, project: str) -> dict[str, dict]:
    """node_key → {badge, reasons, rejected_paths, constraints, minor, last_at} from node_badge_v (empty on an older DB)."""
    try:
        return {r["node_key"]: dict(r) for r in conn.execute(
            "SELECT node_key, badge, reasons, rejected_paths, constraints, minor, last_at FROM node_badge_v WHERE project = ?", (project,))}
    except sqlite3.Error:
        return {}


NEIGHBOURHOOD_MAX_NODES = _psg.NEIGHBOURHOOD_MAX_NODES if _psg is not None else 400
NEIGHBOURHOOD_HOPS = _psg.NEIGHBOURHOOD_HOPS if _psg is not None else 2
# There is deliberately no flat node cap any more: a selection past this many
# nodes is CLUSTERED by module, never cropped (the old 200 drew a quarter of a
# 1601-node selection while the corner still said 1601).
CLUSTER_ABOVE = _psg.CLUSTER_ABOVE if _psg is not None else 600
GRAPH_MODES = _psg.GRAPH_MODES if _psg is not None else ("focus", "story", "data", "full")


def resolve_mode(mode: str | None, focus: str | None) -> str:
    """DP phase 2d: which of the four modes a request means. With a focus the
    default is that focus's neighbourhood; without one it is `story` — the nodes
    that have a story. An unrecognised mode falls back to the same defaults, never
    to `full`: the whole graph is only ever drawn because someone asked for it."""
    if mode in GRAPH_MODES:
        return mode
    return "focus" if focus else "story"


def reason_run(conn: sqlite3.Connection, reason_id: int) -> dict | None:
    """The record `at=reason:<id>` points at: its run and the node it is anchored
    on, so the Graph view can render that run and SAY why (DP phase 2d, Task 0)."""
    try:
        r = conn.execute("SELECT id, run_id, node_key, project, plan_id FROM change_reason_v WHERE id = ?", (int(reason_id),)).fetchone()
    except (sqlite3.Error, TypeError, ValueError):
        return None
    return dict(r) if r else None


def get_graph(conn: sqlite3.Connection, project: str, at: int | str | None = None, level: str = "functions",
              focus: str | None = None, mode: str | None = None, cluster_above: int | None = None) -> dict:
    """The project as it is (or was, at `at`): nodes with their badge (how many
    records have a story) and the tier of their latest event, edges, the run list
    for the selector. `at` is typed (`run:` / `reason:`); a reason id is resolved
    to the run it belongs to and the result says so. `mode` decides how much graph
    is drawn — focus / story / data / full — and the result always carries
    `total_nodes` so a cropped view can name what it cropped. PSG only through
    psg_bridge (ro); a missing graph is `available: False`, never a 500."""
    mode = resolve_mode(mode, focus)
    base = {"project": project, "available": False, "reason": None, "nodes": [], "edges": [], "runs": [], "run": None,
            "edges_from": "none", "level": level, "badged": 0, "focus": focus or None, "mode": mode,
            "total_nodes": 0, "mode_total": 0, "truncated": False, "focus_found": False, "hops": NEIGHBOURHOOD_HOPS,
            "at_kind": None, "at_reason": None, "at_id": None, "story_keys": 0,
            "clusters": [], "layout": "physics"}
    a = parse_at(at)
    base.update(at_kind=a["kind"], at_id=a["id"])
    if _psg is None:
        base["reason"] = "state graph unavailable: provledger.psg_bridge cannot be imported in this environment"
        return base
    db_path = _psg.db_path_for(project)
    if not db_path or not os.path.exists(db_path):
        base["reason"] = f"state graph unavailable: project {project!r} is not registered or its graph file is missing"
        return base
    run_id = a["id"] if a["kind"] == "run" else None
    if a["kind"] == "reason":
        rec = reason_run(conn, a["id"])
        base["at_reason"] = rec
        run_id = rec["run_id"] if rec and rec["run_id"] is not None else None
    badges = node_badges(conn, project)
    # the badge lives in the orchestrator DB, so `story` mode's seeds are computed here —
    # ordered by badge descending so the cap keeps the nodes with the most to say
    story_keys = [k for k, _ in sorted(((k, int(b.get("badge") or 0)) for k, b in badges.items()),
                                       key=lambda kv: (-kv[1], kv[0])) if _ > 0]
    g = _psg.graph_at(db_path, run_id=run_id, level=level if level in ("functions", "full") else "functions",
                      mode=mode, focus=focus, hops=NEIGHBOURHOOD_HOPS, story_keys=story_keys,
                      cluster_above=CLUSTER_ABOVE if cluster_above is None else cluster_above)
    tiers = _psg.latest_tier_of(db_path, run_id=g["run_id"])
    nodes = []
    for n in g["nodes"]:
        # a record may be anchored by node_key or by qualified name (a constraint declared by name): both count
        b1 = badges.get(n["node_key"]) or {}; b2 = badges.get(n["qualified_name"]) or {}
        nodes.append({**n, "badge": int(b1.get("badge") or 0) + int(b2.get("badge") or 0), "minor": int(b1.get("minor") or 0) + int(b2.get("minor") or 0),
                      "tier": tiers.get(n["node_key"], "observed"), "last_at": max([x for x in (b1.get("last_at"), b2.get("last_at")) if x], default=None)})
    runs = _psg.runs_of(db_path)
    run = next((r for r in runs if r["run_id"] == g["run_id"]), None)
    base.update(available=True, nodes=nodes, edges=g["edges"], runs=runs[:50], run=run, edges_from=g["edges_from"],
                level=g["level"], badged=sum(1 for n in nodes if n["badge"]), latest_run_id=g.get("latest_run_id"),
                total_nodes=g.get("total_nodes", len(nodes)), mode_total=g.get("mode_total", len(nodes)),
                truncated=bool(g.get("truncated")),
                focus_found=bool(g.get("focus_found")), hops=g.get("hops", NEIGHBOURHOOD_HOPS),
                mode=g.get("mode", mode), story_keys=len(story_keys),
                clusters=g.get("clusters") or [], layout=g.get("layout", "physics"))
    return base


# ── DP phase 2b (Task 4): the context triple (project, node, at) across the three views ──

AT_PREFIXES = ("run", "reason")


def parse_at(at) -> dict:
    """DP phase 2d (Task 0): `at` says WHAT it points at — `run:<id>` (a state-graph
    analysis run) or `reason:<id>` (one recorded record). 2b shipped a bare id and
    the three views disagreed about what it meant: Node treated it as both, Graph
    always as a run, so the "被 <plan> 采用" link sent a reason id to a run lookup.

    A bare number still reads as a run for one version (compatibility). Anything
    else — a plan id, a malformed prefix — stays untyped rather than being guessed
    into a type it does not have."""
    if at in (None, ""):
        return {"kind": None, "id": None, "at": None}
    s = str(at)
    for kind in AT_PREFIXES:
        if s.startswith(kind + ":"):
            rest = s[len(kind) + 1:]
            return {"kind": kind, "id": int(rest), "at": f"{kind}:{int(rest)}"} if rest.isdigit() \
                else {"kind": None, "id": None, "at": s}
    if s.isdigit():
        return {"kind": "run", "id": int(s), "at": f"run:{int(s)}"}     # compatibility: a bare id was always a run
    return {"kind": None, "id": None, "at": s}


def triple(project: str | None = None, node: str | None = None, at: str | None = None) -> dict:
    """The context every view carries: missing items are simply absent (I12).
    `at` is normalised to its typed form (DP phase 2d) and carries its kind."""
    a = parse_at(at)
    return {"project": project or None, "node": node or None, "at": a["at"],
            "at_kind": a["kind"], "at_id": a["id"]}


def url_for_view(view: str, t: dict, plan_id: str | None = None) -> str | None:
    """graph | node | task → the URL that keeps the triple; None when the view has no anchor to go to."""
    from urllib.parse import quote, urlencode
    q = {}
    project, node, at = t.get("project"), t.get("node"), t.get("at")
    kind = t.get("at_kind", parse_at(at)["kind"])
    if view == "graph":
        if not project:
            return None
        if node:
            q["focus"] = node
        if at and kind in AT_PREFIXES:                  # a typed at: Graph resolves a reason to its run, never guesses
            q["at"] = at
        return f"/graph/{quote(project, safe='')}" + (f"?{urlencode(q)}" if q else "")
    if view == "node":
        if not (project and node):
            return None
        if at:
            q["at"] = at
        return f"/node/{quote(project, safe='')}/{quote(node, safe='')}" + (f"?{urlencode(q)}" if q else "")
    if view == "task":
        pid = plan_id or (at if at and kind is None else None)
        if not pid:
            return "/"
        if node:
            q["node"] = node
        if at and at != pid:
            q["at"] = at
        return f"/plan/{quote(pid, safe='')}" + (f"?{urlencode(q)}" if q else "")
    return None


def view_bar(view: str, t: dict, plan_id: str | None = None) -> dict:
    """What base.html renders: the three links (None when unreachable), the current view, the breadcrumb."""
    return {"current": view, "triple": t, "plan_id": plan_id,
            "links": {v: url_for_view(v, t, plan_id) for v in ("graph", "node", "task")}}


# ── DP phase 2d (Task 3c): the decisions history changed, first ──────────────
# provLedger's whole claim is "a past decision changed this plan". Until now you
# could only find that by scrolling to a stats line on a node page. It is now the
# Task page's first block — and a plan that adopted nothing SAYS so, because a
# hidden block and an empty one read identically and only one is true.

def changed_by_history(conn: sqlite3.Connection, plan_id: str) -> list[dict]:
    """Every record this plan adopted (`influence`), with the words it carries,
    where they were recorded, and how checkable they are. Empty list on an older
    DB — the caller renders the spoken empty state either way."""
    try:
        rows = conn.execute(
            "SELECT i.reason_id, i.via, i.by, i.at, i.step_id AS adopted_in_step, i.node_key AS influence_node, "
            "       r.plan_id AS recorded_in_plan, r.step_id AS recorded_in_step, r.node_key, r.tier, "
            "       r.role, r.recorded_by, r.recorded_at, r.evidence_level, r.run_id, "
            "       r.verbatim_utterance_id, r.verbatim_start, r.verbatim_end, "
            "       COALESCE(substr(u.text, r.verbatim_start + 1, r.verbatim_end - r.verbatim_start), "
            "                r.interpretation, r.statement) AS text, "
            "       p.original_goal AS recorded_in_title "
            "FROM influence i JOIN change_reason_v r ON r.id = i.reason_id "
            "LEFT JOIN utterance u ON u.id = r.verbatim_utterance_id "
            "LEFT JOIN Plans p ON p.plan_id = r.plan_id "
            "WHERE i.plan_id = ? ORDER BY i.id", (plan_id,)).fetchall()
    except sqlite3.Error:
        return []
    # a link should say the NAME of the thing, not its key (layout spec 8) — the
    # key stays in the tooltip, so the ledger's identifier is never lost either
    project = None
    try:
        row = conn.execute("SELECT project FROM Plans WHERE plan_id = ?", (plan_id,)).fetchone()
        project = row[0] if row else None
    except sqlite3.Error:
        project = None
    db_path = _psg.db_path_for(project) if (_psg is not None and project) else None
    # Three influence rows citing the same constraint are three ADOPTIONS of one
    # record, not three decisions. The record is listed once with its count; the
    # individual adoptions (and how each was made) ride along for the expand.
    out: list[dict] = []
    by_id: dict[int, dict] = {}
    for r in rows:
        d = dict(r)
        if d["reason_id"] in by_id:
            g = by_id[d["reason_id"]]
            g["adoptions"] += 1
            g["vias"].append({"via": d.get("via"), "by": d.get("by"), "at": d.get("at"),
                              "step_id": d.get("adopted_in_step")})
            continue
        d["adoptions"] = 1
        d["vias"] = [{"via": d.get("via"), "by": d.get("by"), "at": d.get("at"),
                      "step_id": d.get("adopted_in_step")}]
        by_id[d["reason_id"]] = d
        d["node_key"] = d["node_key"] or d["influence_node"]
        d["qualified_name"] = d["node_key"]
        if db_path and d["node_key"] and str(d["node_key"]).startswith("nk_"):
            try:
                d["qualified_name"] = _psg.latest_qualified_name(db_path, d["node_key"]) or d["node_key"]
            except Exception:
                pass
        d["source_level"] = SOURCE_LEVELS.get(d.get("evidence_level") or "", d.get("evidence_level") or "—")
        d["verbatim"] = d["tier"] == "stated"
        out.append(d)
    return out


def shown_count(conn: sqlite3.Connection, plan_id: str) -> int:
    """How many records this plan was SHOWN. Never derived from adopted (I11)."""
    try:
        return int(conn.execute("SELECT COUNT(*) FROM read_hit WHERE plan_id = ?", (plan_id,)).fetchone()[0])
    except sqlite3.Error:
        return 0


def reason_for_mark(conn: sqlite3.Connection, reason_id: int | None) -> dict | None:
    """One record's verbatim anchor, for marking the cited span on a plan page the
    record was NOT adopted by (following a link from somewhere else)."""
    if reason_id is None:
        return None
    try:
        r = conn.execute("SELECT id AS reason_id, plan_id AS recorded_in_plan, step_id AS recorded_in_step, node_key, tier, "
                         "       verbatim_utterance_id, verbatim_start, verbatim_end "
                         "FROM change_reason_v WHERE id = ?", (int(reason_id),)).fetchone()
    except (sqlite3.Error, TypeError, ValueError):
        return None
    return dict(r) if r else None


SEARCH_LIMIT = 40


def _qualified_for(project: str | None, node_key: str | None) -> str | None:
    """A node's name for a key — a reader wants the name, the key belongs in the
    tooltip (layout spec 8)."""
    if not (project and node_key and str(node_key).startswith("nk_") and _psg is not None):
        return node_key
    db_path = _psg.db_path_for(project)
    if not db_path or not os.path.exists(db_path):
        return node_key
    try:
        return _psg.latest_qualified_name(db_path, node_key) or node_key
    except Exception:
        return node_key


def search_records(conn: sqlite3.Connection, q: str, project: str | None = None) -> tuple[list[dict], bool, int]:
    """(groups, degraded, hits) — records whose words match `q`, grouped by the
    node they are recorded on. `degraded` is True when the FTS5 index was not
    usable (the dashboard reads mode=ro and cannot build it), so the page can
    say the result is a plain substring match rather than imply completeness."""
    like = f"%{q.strip()}%"
    params: list = [like, like]
    where = "(COALESCE(r.interpretation, '') LIKE ? OR COALESCE(r.statement, '') LIKE ?"
    where += " OR COALESCE(substr(u.text, r.verbatim_start + 1, r.verbatim_end - r.verbatim_start), '') LIKE ?)"
    params.append(like)
    if project:
        where += " AND r.project = ?"
        params.append(project)
    try:
        rows = conn.execute(
            "SELECT r.id AS reason_id, r.project, r.node_key, r.plan_id, r.step_id, r.tier, r.role, "
            "       r.recorded_by, r.recorded_at, r.evidence_level, "
            "       COALESCE(substr(u.text, r.verbatim_start + 1, r.verbatim_end - r.verbatim_start), "
            "                r.interpretation, r.statement) AS text, "
            "       p.original_goal AS plan_title "
            "FROM change_reason_v r LEFT JOIN utterance u ON u.id = r.verbatim_utterance_id "
            "LEFT JOIN Plans p ON p.plan_id = r.plan_id "
            f"WHERE {where} AND r.state = 'active' AND r.superseded_by IS NULL "
            "ORDER BY r.id DESC LIMIT ?", (*params, SEARCH_LIMIT)).fetchall()
    except sqlite3.Error:
        return [], True, 0
    grouped: dict[str, dict] = {}
    for r in rows:
        d = dict(r)
        d["source_level"] = SOURCE_LEVELS.get(d.get("evidence_level") or "", "—")
        d["verbatim"] = d["tier"] == "stated"
        key = d["node_key"] or "(没有锚点)"
        g = grouped.setdefault(key, {"node_key": key, "project": d["project"], "hits": [],
                                     "qualified_name": _qualified_for(d["project"], key)})
        g["hits"].append(d)
    # `degraded` is True by construction: the read-only connection cannot build or
    # trust the FTS index, so this is LIKE, and the page says so.
    return list(grouped.values()), True, len(rows)


def _read_hit_counts(conn: sqlite3.Connection, base: dict) -> dict[int, int]:
    """How often each of this node's records was surfaced. Counted from read_hit,
    never inferred from how many rows exist."""
    ids = [r["id"] for r in (base.get("reasons") or []) + (base.get("constraints") or []) if r.get("id")]
    if not ids:
        return {}
    ph = ",".join("?" * len(ids))
    try:
        return {r[0]: r[1] for r in conn.execute(
            f"SELECT reason_id, COUNT(*) FROM read_hit WHERE reason_id IN ({ph}) GROUP BY reason_id", ids)}
    except sqlite3.Error:
        return {}


TRACE_STRIP_MAX = 8


def trace_strip(ledger: dict, limit: int = TRACE_STRIP_MAX) -> list[dict]:
    """The latest moment of each KIND, newest first — at most one structural
    change, one stated reason, one asserted reason, one constraint, one outcome.

    Taking the latest N rows instead put the same repeated sentence in every
    line, which told the reader nothing. Everything past the cut is still in the
    timeline below: this crops a summary, not the record."""
    rows = [r for r in (ledger.get("timeline") or []) if r.get("significant", True)]
    rows.sort(key=lambda r: str(r.get("at") or ""), reverse=True)
    seen: dict[str, dict] = {}
    for r in rows:
        seen.setdefault(_recent_kind(r), r)
    out = [seen[k] for k in RECENT_KINDS if k in seen]
    out.sort(key=lambda r: str(r.get("at") or ""), reverse=True)
    return out[:max(0, min(int(limit), 5))]


def mark_span(text: str | None, start: int | None, end: int | None) -> str:
    """The text with [start, end) wrapped in <mark>, HTML-escaped around it.

    A span that does not fit the text is a data problem, not a reason to mangle
    the quote: the text comes back escaped and unmarked."""
    import html as _html
    if not text:
        return ""
    if start is None or end is None or not (0 <= int(start) < int(end) <= len(text)):
        return _html.escape(text)
    s, e = int(start), int(end)
    return (_html.escape(text[:s]) + '<mark class="bg-brand-spark/30 rounded px-0.5" data-span="1">'
            + _html.escape(text[s:e]) + "</mark>" + _html.escape(text[e:]))


# ── DP phase 2b (Task 5): session cards ─────────────────────────────────────────

_ORCH_RE = re.compile(r"run-step|publish-plan|review_run|complete-step|start-step|deviate|fail-step|finish-plan|record-|ledger-")
_PROV_RE = re.compile(r"reason-|provledger |hooks/|analyzer ")


def _bucket(head: str | None) -> str | None:
    if not head:
        return None
    if _PROV_RE.search(head):
        return "provenance"
    if _ORCH_RE.search(head):
        return "orchestration"
    return None


def get_session(conn: sqlite3.Connection, session_id: str, personal_ok: bool = True) -> dict:
    """One session: what was said (utterances; personal ones marked, shown only on
    this machine), what it cost (tool calls, the two buckets), what changed
    (the session refresh's node events, when there was one), its headline, the
    plans it published. Read-only; an older DB renders empty parts."""
    out = {"session_id": session_id, "found": False, "run": None, "utterances": [], "tool_calls": 0, "buckets": {"orchestration": 0, "provenance": 0, "other": 0},
           "ratios": {"orchestration": None, "provenance": None}, "changed_nodes": [], "headline": None, "plans": [], "degraded": False, "first_at": None, "last_at": None}
    try:
        r = conn.execute("SELECT session_id, project, cwd, started_at, ended_at, psg_run_id, refresh_state, note FROM session_run WHERE session_id = ?", (session_id,)).fetchone()
        out["run"] = dict(r) if r else None
    except sqlite3.Error:
        out["run"] = None
    try:
        rows = conn.execute("SELECT id, project, plan_id, text, occurred_at, visibility FROM utterance WHERE session_id = ? ORDER BY id", (session_id,)).fetchall()
        out["utterances"] = [{**dict(u), "text": (u["text"] if (personal_ok or u["visibility"] != "personal") else "(personal — not shown here)")} for u in rows]
    except sqlite3.Error:
        pass
    try:
        calls = conn.execute("SELECT tool_name, command_head, at FROM tool_call_log WHERE session_id = ? ORDER BY id", (session_id,)).fetchall()
        out["tool_calls"] = len(calls)
        for c in calls:
            b = _bucket(c["command_head"]) if c["tool_name"] == "Bash" else None
            out["buckets"][b or "other"] += 1
        if calls:
            out["first_at"], out["last_at"] = calls[0]["at"], calls[-1]["at"]
            n = len(calls)
            out["ratios"] = {"orchestration": round(out["buckets"]["orchestration"] / n, 4), "provenance": round(out["buckets"]["provenance"] / n, 4)}
    except sqlite3.Error:
        pass
    try:
        out["plans"] = [dict(p) for p in conn.execute("SELECT plan_id, original_goal, status, created_at, project FROM Plans WHERE session_id = ? ORDER BY created_at", (session_id,))]
    except sqlite3.Error:
        out["plans"] = []
    try:
        h = conn.execute("SELECT id, findings_json, computed_at FROM headline WHERE session_id = ? ORDER BY id DESC LIMIT 1", (session_id,)).fetchone()
        if h:
            doc = json.loads(h["findings_json"]); out["headline"] = {"headline_id": h["id"], "computed_at": h["computed_at"], "summary": doc.get("summary") or {}, "findings": doc.get("findings") or []}
    except (sqlite3.Error, ValueError):
        pass
    run = out["run"]
    if run and run.get("psg_run_id") and _psg is not None and run.get("project"):
        db_path = _psg.db_path_for(run["project"])
        if db_path and os.path.exists(db_path):
            try:
                out["changed_nodes"] = _psg.changed_node_keys(db_path, f"session:{session_id}")
            except Exception:
                out["changed_nodes"] = []
    out["found"] = bool(run or out["utterances"] or out["tool_calls"] or out["plans"])
    out["degraded"] = out["found"] and not out["plans"]
    return out


def recent_sessions(conn: sqlite3.Connection, limit: int = 10) -> list[dict]:
    """The last sessions the hooks or the publish saw: id, first/last activity, utterance and tool-call counts, plan count, degraded flag."""
    try:
        rows = conn.execute("""
            SELECT s.session_id, MIN(s.first_at) AS first_at, MAX(s.last_at) AS last_at,
                   SUM(s.utterances) AS utterances, SUM(s.calls) AS calls, SUM(s.plans) AS plans
            FROM (
              SELECT session_id, MIN(occurred_at) AS first_at, MAX(occurred_at) AS last_at, COUNT(*) AS utterances, 0 AS calls, 0 AS plans FROM utterance WHERE session_id <> '' GROUP BY session_id
              UNION ALL
              SELECT session_id, MIN(at), MAX(at), 0, COUNT(*), 0 FROM tool_call_log WHERE session_id <> '' GROUP BY session_id
              UNION ALL
              SELECT session_id, MIN(created_at), MAX(created_at), 0, 0, COUNT(*) FROM Plans WHERE session_id IS NOT NULL GROUP BY session_id
              UNION ALL
              SELECT session_id, started_at, ended_at, 0, 0, 0 FROM session_run
            ) s GROUP BY s.session_id ORDER BY last_at DESC LIMIT ?""", (limit,)).fetchall()
    except sqlite3.Error:
        return []
    out = []
    for r in rows:
        d = dict(r)
        try:
            sr = conn.execute("SELECT refresh_state, project FROM session_run WHERE session_id = ?", (d["session_id"],)).fetchone()
        except sqlite3.Error:
            sr = None
        d["refresh_state"] = sr["refresh_state"] if sr else None
        d["project"] = sr["project"] if sr else None
        d["degraded"] = not d["plans"]
        out.append(d)
    return out
